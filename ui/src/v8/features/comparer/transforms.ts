/**
 * Comparer transforms (pure, tested). Each shown value keeps the exact source value it comes from.
 *
 *  - none: the reported value.
 *  - index: base 100 at the first period where every compared place has an observed value (common base);
 *    unavailable without such a period.
 *  - yoy: change over one year, % ((v / v_{t-1y}) − 1) × 100, same sub-period one year earlier.
 *  - percap: v ÷ population (atlas headline population of the same country and year) — a REVA computation
 *    naming both sources; a year without population is missing ("population manquante"), never filled.
 * A result touching a projection is a projection. Missing stays missing (never 0).
 */
import type { MultiSeries, Point } from '../monde/api';

export type TransformId = 'none' | 'index' | 'yoy' | 'percap';

export const TRANSFORM_LABEL: Record<TransformId, string> = {
  none: 'Valeurs',
  index: 'Base 100',
  yoy: 'Variation sur un an',
  percap: 'Par habitant',
};

export interface PopPoint {
  year: number;
  v: string;
  unit?: string | null;
  series?: string | null;
}

export interface Shown {
  /** Period code ("2020", "2020-Q1", "2020-03"). */
  p: string;
  /** Numeric x for charts (fractional year). */
  x: number;
  /** Shown value (null = missing; `miss` says why). */
  y: number | null;
  proj: boolean;
  miss: string | null;
  src: Point | null;
  /** Inputs of a computed value (base or previous period value, population). */
  note?: string;
  pop?: PopPoint | null;
}

export function xOf(ps: string): number {
  const neg = ps.startsWith('-');
  const [y, m, d] = (neg ? ps.slice(1) : ps).split('-').map(Number) as [number, number, number];
  return (neg ? -y : y) + ((m || 1) - 1) / 12 + ((d || 1) - 1) / 365;
}

/** The same sub-period one year earlier ("2020-Q3" → "2019-Q3", "2020-03" → "2019-03", "2020" → "2019"). */
export function previousYearCode(p: string): string | null {
  const m = /^(-?\d+)(.*)$/.exec(p);
  if (!m) return null;
  return `${Number(m[1]) - 1}${m[2]}`;
}

function yearOf(p: string): number {
  return Number(/^(-?\d+)/.exec(p)?.[1]);
}

/** Common base period: first period where every place has an observed (non-projection) numeric value. */
export function commonBase(places: Record<string, Point[]>): string | null {
  const geos = Object.keys(places);
  if (geos.length === 0) return null;
  const sets = geos.map((g) => new Set((places[g] ?? []).filter((p) => p.v !== null && !p.proj && Number(p.v) !== 0).map((p) => p.p)));
  const first = [...(places[geos[0]!] ?? [])].sort((a, b) => a.ps.localeCompare(b.ps));
  for (const p of first) if (sets.every((s) => s.has(p.p))) return p.p;
  return null;
}

export function transformPlace(
  pts: Point[],
  t: TransformId,
  ctx: { base?: string | null; pop?: PopPoint[] | null; popScale?: number },
): Shown[] {
  const sorted = [...pts].sort((a, b) => a.ps.localeCompare(b.ps));
  const by = new Map(sorted.map((p) => [p.p, p]));
  const pop = new Map((ctx.pop ?? []).map((p) => [p.year, p]));
  return sorted.map((p) => {
    const base: Shown = { p: p.p, x: xOf(p.ps), y: null, proj: p.proj, miss: p.miss ?? null, src: p };
    const v = p.v !== null ? Number(p.v) : null;
    if (v === null || !Number.isFinite(v)) return { ...base, miss: p.miss ?? (p.t ? 'valeur textuelle' : 'non rapporté') };
    if (t === 'none') return { ...base, y: v };
    if (t === 'index') {
      const b = ctx.base ? by.get(ctx.base) : undefined;
      const bv = b?.v !== null && b?.v !== undefined ? Number(b.v) : null;
      if (bv === null || bv === 0) return { ...base, miss: 'pas de valeur de base' };
      return { ...base, y: (v / bv) * 100, note: `base ${ctx.base} = ${b!.v}` };
    }
    if (t === 'yoy') {
      const prevCode = previousYearCode(p.p);
      const prev = prevCode ? by.get(prevCode) : undefined;
      const pv = prev?.v !== null && prev?.v !== undefined ? Number(prev.v) : null;
      if (pv === null || pv === 0) return { ...base, miss: 'pas de valeur un an plus tôt' };
      return { ...base, y: (v / pv - 1) * 100, proj: p.proj || !!prev?.proj, note: `${prevCode} = ${prev!.v}` };
    }
    // percap
    const pp = pop.get(yearOf(p.p));
    if (!pp) return { ...base, miss: 'population manquante pour cette année', pop: null };
    const pv = Number(pp.v) * (ctx.popScale ?? 1);
    if (!(pv > 0)) return { ...base, miss: 'population manquante pour cette année', pop: pp };
    return { ...base, y: v / pv, pop: pp, note: `÷ population ${pp.v}${ctx.popScale && ctx.popScale !== 1 ? ` × ${ctx.popScale}` : ''} (${pp.series ?? 'atlas'})` };
  });
}

/** Multiplier turning the population unit into persons ("thousands" → 1000). */
export function popScale(unit: string | null | undefined): number {
  const u = (unit ?? '').toLowerCase();
  if (/million/.test(u)) return 1e6;
  if (/thousand|milliers/.test(u)) return 1e3;
  return 1;
}

export function shownUnit(s: MultiSeries, t: TransformId): string {
  const u = s.meta.unit ?? '';
  if (t === 'index') return `indice (base 100)`;
  if (t === 'yoy') return '% sur un an';
  if (t === 'percap') return `${u} par habitant`;
  return u;
}

/** Whether transform t applies to series s (server rule, plus a common base for index). */
export function allowed(s: MultiSeries, t: TransformId): { ok: boolean; reason: string | null } {
  if (t === 'none') return { ok: true, reason: null };
  const r = s.transforms[t];
  if (!r || !r.ok) return { ok: false, reason: r?.reason ?? 'non applicable' };
  if (t === 'index' && commonBase(s.places) === null) return { ok: false, reason: 'aucune période observée commune à tous les lieux' };
  return { ok: true, reason: null };
}
