/**
 * Diverging colour scale with a neutral midpoint at zero (blue = negative, red = positive),
 * five steps per arm. Used by the exposure choropleth and the IO price heatmap.
 *
 * Classes are computed from the decimal string (sign and magnitude), so a value is never
 * coloured from a rounded float when it sits exactly on a break. Breaks are fixed per
 * scale (never re-fitted per filter), so a colour always means the same range.
 */
import { compareDecimalStrings } from './format';

export const NEUTRAL = '#f0efec';
/** Index 0 = lightest (nearest zero). */
export const POS_STEPS = ['#f6d3d1', '#eea6a3', '#e34948', '#b8302f', '#7f1d1d'];
export const NEG_STEPS = ['#cde2fb', '#86b6ef', '#3987e5', '#1c5cab', '#0d366b'];
/** Text colour for a filled cell (dark fills get light text). */
export const DARK_FILL = new Set([POS_STEPS[2], POS_STEPS[3], POS_STEPS[4], NEG_STEPS[2], NEG_STEPS[3], NEG_STEPS[4]]);

export interface DivergingScale {
  /** Ascending positive break points as decimal strings (5 arm steps -> 4 inner breaks). */
  breaks: string[];
  unit: string;
}

/** Exposure (% of baseline GDP): breaks 0.25, 1, 2.5, 5. */
export const EXPOSURE_SCALE: DivergingScale = { breaks: ['0.25', '1', '2.5', '5'], unit: '%' };

export interface ScaleClass {
  /** -5..-1 negative arm, 0 exactly zero, 1..5 positive arm. */
  step: number;
  color: string;
  label: string;
}

function abs(v: string): string {
  return v.trim().startsWith('-') ? v.trim().slice(1) : v.trim();
}

function isZero(v: string): boolean {
  return /^[-+]?0*(\.0*)?$/.test(v.trim());
}

/** The class of a decimal string under a scale; null for a non-numeric string. */
export function classify(value: string, scale: DivergingScale): ScaleClass | null {
  if (!/^[-+]?(\d+\.?\d*|\.\d+)([eE][-+]?\d+)?$/.test(value.trim())) return null;
  if (isZero(value)) return { step: 0, color: NEUTRAL, label: `0 ${scale.unit}` };
  const neg = value.trim().startsWith('-');
  const a = abs(value);
  let i = 0;
  while (i < scale.breaks.length && compareDecimalStrings(a, scale.breaks[i]!) !== -1) i++;
  const step = i + 1;
  return { step: neg ? -step : step, color: (neg ? NEG_STEPS : POS_STEPS)[i]!, label: legendLabel(step, neg, scale) };
}

/**
 * Label of an arm step. Magnitude ranges include their lower bound and exclude their upper
 * bound (stated in the legend caption); step 1 excludes zero, which has its own class.
 */
function legendLabel(step: number, neg: boolean, scale: DivergingScale): string {
  const s = neg ? '−' : '+';
  const lo = step === 1 ? null : scale.breaks[step - 2]!;
  const hi = step <= scale.breaks.length ? scale.breaks[step - 1]! : null;
  if (hi === null) return `${s}${lo} ${scale.unit} or beyond`;
  if (lo === null) return `${s}0 to ${s}${hi} ${scale.unit} (not 0)`;
  return `${s}${lo} to ${s}${hi} ${scale.unit}`;
}

/** Legend rows, from most negative to most positive. */
export function legendRows(scale: DivergingScale): ScaleClass[] {
  const n = scale.breaks.length + 1;
  const neg: ScaleClass[] = [];
  const pos: ScaleClass[] = [];
  for (let step = 1; step <= n; step++) {
    neg.unshift({ step: -step, color: NEG_STEPS[step - 1]!, label: legendLabel(step, true, scale) });
    pos.push({ step, color: POS_STEPS[step - 1]!, label: legendLabel(step, false, scale) });
  }
  return [...neg, { step: 0, color: NEUTRAL, label: `exactly 0 ${scale.unit}` }, ...pos];
}

const NICE = [1, 2, 2.5, 5, 10];

/**
 * A fixed scale for a data set whose magnitude is not known in advance (e.g. IO price
 * changes): four breaks at 1/8, 1/4, 1/2 and 1 of a "nice" ceiling of the largest |value|.
 * Breaks are printed in the legend, so the reader sees exactly what each colour spans.
 */
export function niceScale(values: string[], unit: string): DivergingScale {
  let max = 0;
  for (const v of values) {
    const n = Math.abs(Number(v));
    if (Number.isFinite(n) && n > max) max = n;
  }
  if (max === 0) return { breaks: ['0.125', '0.25', '0.5', '1'], unit };
  const exp = Math.floor(Math.log10(max));
  const base = 10 ** exp;
  const m = NICE.find((x) => x * base >= max) ?? 10;
  const top = m * base;
  const fmt = (x: number) => String(Number(x.toPrecision(6)));
  return { breaks: [fmt(top / 8), fmt(top / 4), fmt(top / 2), fmt(top)], unit };
}
