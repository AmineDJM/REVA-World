/**
 * Choropleth classes for Monde. Colour only encodes the class; the exact value is in the tooltip and ranking.
 *
 *  - quantile: classes hold about the same number of places (skewed distributions);
 *  - linear: equal intervals (near-uniform spread, few values);
 *  - log: equal intervals of log10 (strictly positive values spanning > 3 orders of magnitude);
 *  - diverging: both signs present → two hues around a neutral grey at 0, symmetric (bounds = quantiles of |v|).
 * `auto` takes the server's hint (scale_hint), computed from the observed values of the slice.
 * Sequential = one hue light→dark (blue ramp, validated in light and dark); diverging = red ↔ blue + grey.
 */
export type ScaleMode = 'quantile' | 'linear' | 'log' | 'diverging';
export type ScaleChoice = ScaleMode | 'auto';

export const SCALE_LABEL: Record<ScaleMode, string> = {
  quantile: 'quantiles',
  linear: 'linéaire',
  log: 'logarithmique',
  diverging: 'divergente (0 au centre)',
};

/** Sequential blue ramp (steps 150…700 light; dark: dark→light so small values recede into the dark surface). */
export const SEQ_LIGHT = ['#cde2fb', '#9ec5f4', '#6da7ec', '#3987e5', '#256abf', '#184f95', '#0d366b'];
export const SEQ_DARK = ['#123a6a', '#184f95', '#1c5cab', '#2a78d6', '#5598e7', '#86b6ef', '#b7d3f6'];
/** Diverging: red arm (negative) · neutral grey · blue arm (positive). */
export const DIV_LIGHT = { neg: ['#9e2a2a', '#c43a3a', '#e66767', '#f2a5a5'], mid: '#f0efec', pos: ['#9ec5f4', '#5598e7', '#256abf', '#104281'] };
export const DIV_DARK = { neg: ['#f2a5a5', '#e66767', '#c43a3a', '#7d2424'], mid: '#383835', pos: ['#1c4d8a', '#2a78d6', '#5598e7', '#9ec5f4'] };

export interface Classes {
  mode: ScaleMode;
  /** Upper bounds of classes 0..n-2 (a value v is in class i when breaks[i-1] < v <= breaks[i]). */
  breaks: number[];
  /** Number of classes. */
  n: number;
  min: number;
  max: number;
  /** Sorted values (legend ends show actually reported values). */
  values: number[];
}

export function quantileBreaks(sorted: number[], k: number): number[] {
  const distinct = [...new Set(sorted)];
  if (distinct.length <= k) return distinct.slice(0, -1);
  const out: number[] = [];
  for (let i = 1; i < k; i++) {
    const q = sorted[Math.min(sorted.length - 1, Math.floor((i * sorted.length) / k))]!;
    if ((out.length === 0 || q > out[out.length - 1]!) && q < distinct[distinct.length - 1]!) out.push(q);
  }
  return out;
}

function equalBreaks(lo: number, hi: number, k: number, f: (x: number) => number = (x) => x, inv: (x: number) => number = (x) => x): number[] {
  const a = f(lo);
  const b = f(hi);
  if (!(b > a)) return [];
  return Array.from({ length: k - 1 }, (_, i) => inv(a + ((i + 1) * (b - a)) / k));
}

/** The mode actually used: `auto` → hint; `log` impossible with values ≤ 0 → quantile; `diverging` needs both signs. */
export function effectiveMode(choice: ScaleChoice, hint: string, values: number[]): ScaleMode {
  const lo = values.length ? Math.min(...values) : 0;
  const hi = values.length ? Math.max(...values) : 0;
  let m: ScaleMode = choice === 'auto' ? ((['quantile', 'linear', 'log', 'diverging'] as const).find((x) => x === hint) ?? 'quantile') : choice;
  if (m === 'log' && !(lo > 0)) m = 'quantile';
  if (m === 'diverging' && !(lo < 0 && hi > 0)) m = 'quantile';
  return m;
}

export function classify(values: number[], mode: ScaleMode, k = 7): Classes {
  const v = values.filter((x) => Number.isFinite(x)).sort((a, b) => a - b);
  const min = v[0] ?? 0;
  const max = v[v.length - 1] ?? 0;
  let breaks: number[];
  if (mode === 'quantile') breaks = quantileBreaks(v, Math.min(k, 6));
  else if (mode === 'linear') breaks = new Set(v).size <= 6 ? [...new Set(v)].slice(0, -1) : equalBreaks(min, max, 6);
  else if (mode === 'log') breaks = equalBreaks(min, max, k, Math.log10, (x) => 10 ** x);
  else breaks = divergingBreaks(v);
  return { mode, breaks, n: breaks.length + 1, min, max, values: v };
}

/**
 * Diverging breaks, symmetric around 0: the arm bounds are quantiles of |v| (fifths), so one extreme value
 * (inflation 250 % next to 2–10 % elsewhere) does not flatten every other place into the neutral class — the
 * live-data « empty map » (V8QA). The neutral class holds |v| ≤ the first fifth; each arm has 4 classes.
 */
export function divergingBreaks(sorted: number[]): number[] {
  const abs = sorted.map(Math.abs).filter((x) => x > 0).sort((a, b) => a - b);
  const arm: number[] = [];
  for (let i = 1; i <= 4; i++) {
    const q = abs[Math.floor((i * (abs.length - 1)) / 5)];
    if (q !== undefined && q > 0 && (arm.length === 0 || q > arm[arm.length - 1]!)) arm.push(q);
  }
  if (arm.length === 0) {
    const m = abs[abs.length - 1] ?? 1;
    arm.push(m / 8, m / 4, m / 2, (3 * m) / 4);
  }
  return [...arm.map((x) => -x).reverse(), ...arm];
}

export function classOf(c: Classes, x: number): number {
  let i = 0;
  while (i < c.breaks.length && x > c.breaks[i]!) i++;
  return i;
}

/** Colours of the classes for a theme. */
export function classColors(c: Classes, dark: boolean): string[] {
  if (c.mode === 'diverging') {
    const d = dark ? DIV_DARK : DIV_LIGHT;
    const k = Math.max(0, Math.min(4, (c.n - 1) / 2));
    return [...d.neg.slice(4 - k), d.mid, ...d.pos.slice(0, k)];
  }
  const ramp = dark ? SEQ_DARK : SEQ_LIGHT;
  if (c.n >= ramp.length) return ramp.slice(0, c.n);
  // spread n classes over the ramp, keeping both ends
  if (c.n === 1) return [ramp[Math.floor(ramp.length / 2)]!];
  return Array.from({ length: c.n }, (_, i) => ramp[Math.round((i * (ramp.length - 1)) / (c.n - 1))]!);
}

export function colorOf(c: Classes, colors: string[], x: number): string {
  return colors[Math.min(colors.length - 1, classOf(c, x))]!;
}

/** Legend rows: colour + range (quantile/linear: the reported values at both ends of the class). */
export function legendRows(c: Classes, colors: string[], fmt: (x: number) => string): { color: string; label: string; count: number }[] {
  const rows: { color: string; label: string; count: number }[] = [];
  for (let i = 0; i < c.n; i++) {
    const lo = i === 0 ? -Infinity : c.breaks[i - 1]!;
    const hi = i === c.n - 1 ? Infinity : c.breaks[i]!;
    const inClass = c.values.filter((x) => x > lo && x <= hi);
    let label: string;
    if (c.mode === 'diverging' || c.mode === 'log') {
      const a = Number.isFinite(lo) ? fmt(lo) : null;
      const b = Number.isFinite(hi) ? fmt(hi) : null;
      label = a && b ? `${a} – ${b}` : a ? `> ${a}` : b ? `≤ ${b}` : 'toutes';
    } else if (inClass.length) {
      const a = inClass[0]!;
      const z = inClass[inClass.length - 1]!;
      label = a === z ? fmt(a) : `${fmt(a)} – ${fmt(z)}`;
    } else label = 'aucun lieu';
    rows.push({ color: colors[i]!, label, count: inClass.length });
  }
  return rows;
}
