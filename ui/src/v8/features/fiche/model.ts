/**
 * Pure helpers of the Fiche (testable without DOM): section list with counts, source-chip props, citation text,
 * point conversion for MiniLine, grouping of series by theme, French labels.
 */
import type { SourceInfo } from '../../shell/ui';
import type { Bundle, Point, Source } from './api';

export interface SectionDef {
  id: string;
  label: string;
  count: number | null;
}

/** The in-page sections, in order, with the counts shown on their tabs (null = no count). */
export function sections(b: Bundle): SectionDef[] {
  const relCount = b.relations.reduce((n, g) => n + g.count, 0) + b.related_forms.reduce((n, f) => n + f.relations.reduce((m, g) => m + g.count, 0), 0);
  const placeCount = b.space.chain.length + (b.space.point ? 1 : 0) + b.time.length;
  return [
    { id: 'faits', label: 'Faits', count: b.facts.length },
    { id: 'relations', label: 'Relations', count: relCount },
    { id: 'series', label: 'Séries', count: b.series.length + b.tops.length },
    { id: 'carte', label: 'Carte & temps', count: placeCount },
    { id: 'causes', label: 'Causes & effets', count: b.claims.n_in + b.claims.n_out + b.effects.items.length },
    { id: 'previsions', label: 'Prévisions', count: b.forecasts.n_total },
    { id: 'manques', label: 'Manques', count: b.missing.length },
    { id: 'sources', label: 'Sources', count: b.sources.length },
  ];
}

const VERIF_FR: Record<string, string> = {
  community_edited_not_verified: 'Base collaborative : non vérifié par REVA',
  source_reported_not_verified: 'Rapporté par la source, non vérifié par REVA',
  derived: 'Calculé par REVA à partir d’entrées sourcées (voir la méthode)',
  outside_atlas_snapshot: 'Lu dans la table publiée la plus récente, pas encore épinglée dans l’instantané atlas',
  reva_grouping: 'Regroupement fait par REVA (page d’ancrage), pas une déclaration de source',
};

/** SourceChip props of a bundle source (licence, period, provenance lines). */
export function chip(s: Source | null | undefined): SourceInfo | null {
  if (!s) return null;
  const details: string[] = [];
  if (s.series) details.push(`Série : ${s.series}`);
  else if (s.dataset) details.push(`Jeu : ${s.dataset}`);
  if (s.property) details.push(`Propriété : ${s.property}`);
  if (s.statement_key) details.push(`Déclaration : ${s.statement_key.length > 80 ? `${s.statement_key.slice(0, 77)}…` : s.statement_key}`);
  if (s.raw_sha256) details.push(`Artefact brut : ${s.raw_sha256.slice(0, 12)}…`);
  if (s.selector && Object.keys(s.selector).length) details.push(`Sélecteur : ${Object.entries(s.selector).map(([k, v]) => `${k}=${String(v)}`).join(', ').slice(0, 90)}`);
  if (s.verification && VERIF_FR[s.verification]) details.push(VERIF_FR[s.verification]!);
  if (s.origin === 'test_fixture') details.push('Donnée de test (test_fixture)');
  return {
    source: s.source,
    label: s.label,
    href: s.href,
    licence: s.licence?.licence ? { ...s.licence, licence: s.licence.licence } : null,
    retrievedAt: s.retrieved_at,
    period: s.period,
    details,
  };
}

/** MiniLine points (null = gap, never 0). */
export function linePoints(points: Point[]): { x: number; y: number | null }[] {
  return points.map((p) => {
    const n = p.v === null ? null : Number(p.v);
    return { x: p.x, y: n !== null && Number.isFinite(n) ? n : null };
  });
}

/** First x from which the points are projections (dashed), if any. */
export function projectionSplit(points: Point[]): number | undefined {
  const p = points.find((q) => q.projection);
  return p ? p.x : undefined;
}

/** Citation text of the page: what, where, which sources and licences, when. */
export function citation(b: Bundle, url: string, today: string): string {
  const id = b.identity;
  const name = id?.label ?? b.canonical.id;
  const kind = id?.kind_label_fr ?? b.canonical.kind;
  const srcs = b.sources
    .slice(0, 8)
    .map((s) => `${s.label}${s.licences.length ? ` (${s.licences.join(', ')})` : ''}`)
    .join(' ; ');
  const ids = (id?.ids ?? [])
    .slice(0, 3)
    .map((i) => `${i.scheme} ${i.value}`)
    .join(', ');
  return `« ${name} » (${kind}${ids ? ` — ${ids}` : ''}). Fiche REVA, ${url}, consultée le ${today}. Sources : ${srcs || 'aucune'}.${id?.anchor ? ' Page d’ancrage REVA : regroupement fait par REVA, chaque valeur garde sa source.' : ''}`;
}

/** Series grouped by theme label, in first-seen order. */
export function byTheme<T extends { theme_fr: string | null }>(items: T[]): [string, T[]][] {
  const m = new Map<string, T[]>();
  for (const s of items) {
    const k = s.theme_fr ?? 'Séries';
    if (!m.has(k)) m.set(k, []);
    m.get(k)!.push(s);
  }
  return [...m.entries()];
}

export const STATUS_FR: Record<string, string> = {
  pending: 'en attente',
  resolved: 'résolue',
  void: 'annulée',
  not_prospective: 'non prospective',
};

export const SECTION_FR: Record<string, string> = {
  facts: 'Faits',
  series: 'Séries',
  forecasts: 'Prévisions',
  relations: 'Relations',
  atlas: 'Atlas',
};

/** Grade badge tone: A/B strong, C medium, D/E weak. */
export function gradeTone(g: string | null): 'win' | 'neutral' | 'loss' | 'info' {
  if (!g) return 'info';
  if (g === 'A' || g === 'B') return 'win';
  if (g === 'C') return 'neutral';
  return 'loss';
}

/** Readable short id of a KG id for chips ("chembl:CHEMBL941" → "CHEMBL941"). */
export function shortId(id: string): string {
  return id.includes(':') ? id.slice(id.indexOf(':') + 1) : id;
}

/** Identifiers without repeats (the same scheme + value from two sources is listed once). */
export function uniqueIds<T extends { scheme: string; value: string }>(ids: T[]): T[] {
  const seen = new Set<string>();
  return ids.filter((i) => {
    const k = `${i.scheme.toUpperCase()}|${i.value}`;
    if (seen.has(k)) return false;
    seen.add(k);
    return true;
  });
}

const UNIT_FR: Record<string, string> = {
  persons: 'personnes',
  person: 'personne',
  years: 'ans',
  year: 'an',
  '% change': '% (variation)',
  'US dollars': 'US$',
  'US dollars per person': 'US$ par habitant',
  'international dollars per person': '$ int. par habitant',
  'tonnes per person': 't par habitant',
  'persons per km2 of land area': 'hab./km²',
  'per 100,000 population': 'pour 100 000 hab.',
  'index (0-100)': 'indice (0–100)',
  'index (0-1)': 'indice (0–1)',
  '% of GDP': '% du PIB',
  km2: 'km²',
  'thousand barrels per day': 'kb/j',
  'national currency per month (named per cell)': 'monnaie nationale / mois',
};

/** French display of the common atlas unit labels (the source's unit is kept in the source chip). */
export function unitFr(u: string | null | undefined): string | null {
  if (!u) return null;
  return UNIT_FR[u] ?? u;
}

/** Big magnitudes in headline numbers: « 3 366 Md » (display rounding; the exact value is in the source chip). */
export function bigFr(v: string | number): string {
  const n = typeof v === 'number' ? v : Number(v);
  if (!Number.isFinite(n)) return String(v);
  const a = Math.abs(n);
  const f = (x: number, u: string) => `${(n < 0 ? -x : x).toLocaleString('fr-FR', { maximumFractionDigits: x < 10 ? 2 : x < 100 ? 1 : 0 }).replace('-', '−')} ${u}`;
  if (a >= 1e12) return f(a / 1e12, 'T');
  if (a >= 1e9) return f(a / 1e9, 'Md');
  if (a >= 1e7) return f(a / 1e6, 'M');
  return n.toLocaleString('fr-FR', { maximumFractionDigits: a >= 1000 ? 0 : a >= 10 ? 1 : 2 }).replace('-', '−');
}
