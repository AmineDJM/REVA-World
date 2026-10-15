/**
 * Atlas choropleth rules (tested in choropleth.test.ts):
 *  - a country is coloured only from a cell the API returned for (concept, year);
 *  - a country listed in `countries_without_value` (or a cell without a value) is a *gap*: hatched,
 *    with its reason — never coloured, never zero;
 *  - a display polygon that cannot be matched to an ISO3 key is "hors référentiel" (light grey);
 *  - numeric classes are quantiles of the reported values of that year (fixed for the year shown);
 *    textual concepts get one colour per category.
 */
import type { AtlasCell } from '../api/atlas';
import { flagLines, frValue, missingReasonFr, toNumber } from './format';

export const SEQ = ['#e8f1fb', '#b9d4f1', '#7fb0e3', '#3f84cc', '#1c5aa0', '#0b3466'];
export const CATEGORICAL = ['#3f84cc', '#e07b39', '#4caf7a', '#b65fc5', '#d4b13a', '#d9534f', '#2bb3b1', '#8d6e63', '#7986cb', '#a0a83a'];
export const GAP_INK = '#6b4e16';
export const OUTSIDE = '#eef0f3';

/** Natural Earth ADM0_A3 codes that differ from the ISO 3166 alpha-3 / crosswalk key (display join only). */
export const ADM0_TO_ISO3: Record<string, string> = { SDS: 'SSD', KOS: 'XKX', SAH: 'ESH', PSX: 'PSE' };

/** "FRA:43" (feature_key = ADM0_A3 + ":" + index) -> "FRA". Features without an ISO key return null. */
export function iso3OfFeatureKey(featureKey: string | null | undefined): string | null {
  if (!featureKey) return null;
  const code = featureKey.split(':')[0]?.toUpperCase() ?? '';
  if (!/^[A-Z]{3}$/.test(code) || code === '-99') return null;
  return ADM0_TO_ISO3[code] ?? code;
}

export interface NumericScale {
  kind: 'numeric';
  /** Ascending upper bounds of classes 0..n-2 (last class is open). */
  breaks: number[];
  colors: string[];
  min: number;
  max: number;
}

export interface CategoricalScale {
  kind: 'categorical';
  categories: { value: string; color: string; count: number }[];
}

export type Scale = NumericScale | CategoricalScale;

export function quantileBreaks(values: number[], classes = SEQ.length): number[] {
  const v = [...values].sort((a, b) => a - b);
  if (v.length === 0) return [];
  const out: number[] = [];
  for (let i = 1; i < classes; i++) {
    const q = v[Math.min(v.length - 1, Math.floor((i * v.length) / classes))]!;
    if (out.length === 0 || q > out[out.length - 1]!) out.push(q);
  }
  return out;
}

export function buildScale(cells: AtlasCell[]): Scale | null {
  const nums = cells.map((c) => toNumber(c.value)).filter((n): n is number => n !== null);
  if (nums.length > 0) {
    const breaks = quantileBreaks(nums);
    const colors = SEQ.slice(SEQ.length - (breaks.length + 1));
    return { kind: 'numeric', breaks, colors, min: Math.min(...nums), max: Math.max(...nums) };
  }
  const counts = new Map<string, number>();
  for (const c of cells) if (c.value_text) counts.set(c.value_text, (counts.get(c.value_text) ?? 0) + 1);
  if (counts.size === 0) return null;
  const cats = [...counts.entries()].sort((a, b) => b[1] - a[1]);
  return {
    kind: 'categorical',
    categories: cats.map(([value, count], i) => ({ value, count, color: i < CATEGORICAL.length ? CATEGORICAL[i]! : '#9aa4b2' })),
  };
}

export function colorFor(cell: AtlasCell, scale: Scale | null): string | null {
  if (!scale) return null;
  if (scale.kind === 'numeric') {
    const n = toNumber(cell.value);
    if (n === null) return null;
    let i = 0;
    while (i < scale.breaks.length && n > scale.breaks[i]!) i++;
    return scale.colors[i] ?? scale.colors[scale.colors.length - 1] ?? null;
  }
  if (!cell.value_text) return null;
  return scale.categories.find((c) => c.value === cell.value_text)?.color ?? '#9aa4b2';
}

export type CountryState =
  | { state: 'value'; color: string; cell: AtlasCell; text: string }
  | { state: 'gap'; reason: string; cell: AtlasCell | null }
  | { state: 'outside' };

export const NO_SOURCE_REASON = 'aucune source déclarée ne rapporte cette cellule (absent, pas zéro)';

/** Display text of a cell ("68,2 %", or the reported category). */
export function cellText(c: AtlasCell): string {
  if (c.value !== null) return `${frValue(c.value, c.unit)} ${c.unit === '1' ? '' : c.unit}`.trim();
  if (c.value_text) return c.value_text;
  return '';
}

/**
 * State of every ISO3 for one map response: value (coloured), gap (hatched + reason) or outside
 * (not in the referential of this map). `countriesWithout` comes from the API, it is never inferred
 * from an absent polygon.
 */
export function countryStates(values: AtlasCell[], countriesWithout: string[], scale: Scale | null): Map<string, CountryState> {
  const out = new Map<string, CountryState>();
  for (const iso of countriesWithout) out.set(iso.toUpperCase(), { state: 'gap', reason: NO_SOURCE_REASON, cell: null });
  for (const c of values) {
    if (!c.iso3) continue;
    const iso = c.iso3.toUpperCase();
    const color = colorFor(c, scale);
    if (color && (c.value !== null || c.value_text)) {
      out.set(iso, { state: 'value', color, cell: c, text: cellText(c) });
    } else {
      const reason = typeof c.flags.missing === 'string' ? missingReasonFr(c.flags.missing) : 'cellule sans valeur rapportée';
      out.set(iso, { state: 'gap', reason, cell: c });
    }
  }
  return out;
}

export function tooltipFor(name: string, st: CountryState | undefined, year: number): string {
  if (!st || st.state === 'outside') return `${name} — hors du référentiel de cette carte`;
  if (st.state === 'gap') return `${name} — donnée manquante ${year} : ${st.reason}`;
  const flags = flagLines(st.cell.flags);
  return `${name} — ${st.text} (${st.cell.year ?? year}) · source ${st.cell.series}${flags.length ? ` · ${flags.join(', ')}` : ''}`;
}

export function legendRows(scale: Scale | null, unit: string): { color: string; label: string }[] {
  if (!scale) return [];
  if (scale.kind === 'categorical') return scale.categories.slice(0, CATEGORICAL.length).map((c) => ({ color: c.color, label: `${c.value} (${c.count})` }));
  const f = (n: number) => frValue(String(Number(n.toPrecision(6))), unit);
  const rows: { color: string; label: string }[] = [];
  const b = scale.breaks;
  for (let i = 0; i <= b.length; i++) {
    const lo = i === 0 ? scale.min : b[i - 1]!;
    const hi = i === b.length ? scale.max : b[i]!;
    rows.push({ color: scale.colors[i]!, label: `${i === 0 ? '' : '> '}${f(lo)} – ${f(hi)}` });
  }
  return rows;
}

/** Ranking rows: numeric values descending (exact strings kept for display). */
export function ranking(values: AtlasCell[]): AtlasCell[] {
  return values
    .filter((c) => c.iso3 && (c.value !== null || c.value_text))
    .sort((a, b) => {
      const x = toNumber(a.value);
      const y = toNumber(b.value);
      if (x !== null && y !== null) return y - x;
      if (x !== null) return -1;
      if (y !== null) return 1;
      return (a.value_text ?? '').localeCompare(b.value_text ?? '', 'fr');
    });
}
