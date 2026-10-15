/**
 * Pure extraction of multi-period (V2) simulation results into chartable series, population
 * pyramids, input-output heatmaps and back-test tables.
 *
 * Contracts followed (src/reva/models/*):
 * - a multi-period row carries a half-open period [period_start, period_end) (UTC dates);
 *   its year is the year of period_start. Rows without a period are single-period;
 * - L1 = the stated assumption set; "L2:<name>" = one grid variant; "L2:sensitivity_range" =
 *   min/max over the grid and the L1 point (assumption sensitivity, never a confidence
 *   interval);
 * - demography.cohort_component single ages: population.<female|male>.age_NNN[_plus];
 * - economic.io_price: industry_price_change_percent:<IND>, household_basket_price_change_percent,
 *   household_basket_contribution_<domestic|imported>_products_percent and _<domestic|foreign>_shocks_percent.
 *
 * Values stay decimal strings; numbers are derived only to place marks on a chart.
 */
import type { SimResult, SimulationStatus } from '../api/types';
import type { PeriodResult } from '../api/v2types';
import { isRecord } from './format';

export const RANGE_VARIANT = 'L2:sensitivity_range';

export interface Period {
  start: string;
  end: string;
  year: number;
}

/** The result's period from the row (period_start/period_end), or a {period:{start,end}} fallback. */
export function periodOf(r: SimResult): Period | null {
  const pr = r as PeriodResult & { period?: unknown };
  let start: unknown = pr.period_start;
  let end: unknown = pr.period_end;
  const alt = isRecord(pr.period) ? pr.period : isRecord(r.applicability?.period) ? (r.applicability.period as Record<string, unknown>) : null;
  if ((!start || !end) && alt) {
    start = alt.start;
    end = alt.end;
  }
  if (typeof start !== 'string' || typeof end !== 'string') return null;
  const y = Number(start.slice(0, 4));
  return Number.isFinite(y) ? { start: start.slice(0, 10), end: end.slice(0, 10), year: y } : null;
}

export function countryOf(r: SimResult): string {
  return typeof r.applicability?.country === 'string' ? r.applicability.country : (r.subject?.label ?? '—');
}

export function isMultiPeriod(results: SimResult[]): boolean {
  return results.some((r) => periodOf(r) !== null);
}

export function modelKeyOf(st: SimulationStatus | null | undefined): string | null {
  const k = st?.scenario?.spec?.model_key;
  return typeof k === 'string' ? k : null;
}

// ------------------------------------------------------------------ series

export interface SeriesPoint {
  year: number;
  period: Period;
  /** Exact decimal string, or null for a typed missing value. */
  value: string | null;
  missingReason?: string | null;
  result: SimResult;
}

export interface RangePoint {
  year: number;
  lower: string;
  upper: string;
  statement: string | null;
  oneSidedness: string | null;
  result: SimResult;
}

export interface Series {
  country: string;
  outputKey: string;
  unitRef: string | null;
  l1: SeriesPoint[];
  range: RangePoint[];
  /** Grid variant names present (L2 only). */
  variants: string[];
}

/** Every (country, output_key) multi-period series, L1 points plus the sensitivity range per year. */
export function buildSeries(results: SimResult[], filter?: (outputKey: string) => boolean): Series[] {
  const by = new Map<string, Series>();
  for (const r of results) {
    if (filter && !filter(r.output_key)) continue;
    const p = periodOf(r);
    if (!p) continue;
    const country = countryOf(r);
    const k = `${country}|${r.output_key}`;
    let s = by.get(k);
    if (!s) {
      s = { country, outputKey: r.output_key, unitRef: null, l1: [], range: [], variants: [] };
      by.set(k, s);
    }
    if (r.variant === 'L1') {
      const v = r.value;
      if (v.kind === 'decimal') {
        s.unitRef = v.unit_ref;
        s.l1.push({ year: p.year, period: p, value: v.value, result: r });
      } else if (v.kind === 'missing') {
        s.l1.push({ year: p.year, period: p, value: null, missingReason: String(v.reason), result: r });
      }
    } else if (r.variant === RANGE_VARIANT && r.value.kind === 'sensitivity_range') {
      const v = r.value as typeof r.value & { one_sidedness?: string };
      s.unitRef = s.unitRef ?? v.unit_ref;
      s.range.push({ year: p.year, lower: v.lower, upper: v.upper, statement: v.statement ?? null, oneSidedness: v.one_sidedness ?? null, result: r });
    } else if (r.variant.startsWith('L2:') && !s.variants.includes(r.variant)) {
      s.variants.push(r.variant);
    }
  }
  for (const s of by.values()) {
    s.l1.sort((a, b) => a.year - b.year);
    s.range.sort((a, b) => a.year - b.year);
    s.variants.sort();
  }
  return [...by.values()].sort((a, b) => a.country.localeCompare(b.country) || a.outputKey.localeCompare(b.outputKey));
}

// ------------------------------------------------------------------ population pyramid

export interface PyramidBand {
  label: string;
  /** Lower age of the band (for ordering). */
  lo: number;
  female: string | null;
  male: string | null;
}

export interface Pyramid {
  year: number;
  kind: 'single_age' | 'broad_groups';
  bands: PyramidBand[];
}

const SINGLE_AGE = /^population\.(female|male)\.age_(\d{3})(_plus)?$/;
/** Disjoint, exhaustive broad groups (the published broad outputs include overlapping ones). */
const BROAD = [
  { key: 'age_0_14', label: '0–14', lo: 0 },
  { key: 'age_15_64', label: '15–64', lo: 15 },
  { key: 'age_65_plus', label: '65+', lo: 65 },
];

/** Years for which a pyramid can be drawn (single ages preferred, else broad groups). */
export function pyramidYears(results: SimResult[], country: string, variant = 'L1'): number[] {
  const ys = new Set<number>();
  for (const r of results) {
    if (r.variant !== variant || countryOf(r) !== country) continue;
    if (!SINGLE_AGE.test(r.output_key) && !/^population\.(female|male)\.age_(0_14|15_64|65_plus)$/.test(r.output_key)) continue;
    const p = periodOf(r);
    if (p) ys.add(p.year);
  }
  return [...ys].sort((a, b) => a - b);
}

export function pyramidFor(results: SimResult[], country: string, year: number, variant = 'L1'): Pyramid | null {
  const single = new Map<number, PyramidBand>();
  const broad = new Map<string, PyramidBand>();
  for (const r of results) {
    if (r.variant !== variant || countryOf(r) !== country) continue;
    const p = periodOf(r);
    if (!p || p.year !== year) continue;
    const val = r.value.kind === 'decimal' ? r.value.value : null;
    const m = SINGLE_AGE.exec(r.output_key);
    if (m) {
      const age = Number(m[2]);
      const b = single.get(age) ?? { label: m[3] ? `${age}+` : String(age), lo: age, female: null, male: null };
      b[m[1] as 'female' | 'male'] = val;
      single.set(age, b);
      continue;
    }
    const g = /^population\.(female|male)\.(age_0_14|age_15_64|age_65_plus)$/.exec(r.output_key);
    if (g) {
      const def = BROAD.find((x) => x.key === g[2])!;
      const b = broad.get(def.key) ?? { label: def.label, lo: def.lo, female: null, male: null };
      b[g[1] as 'female' | 'male'] = val;
      broad.set(def.key, b);
    }
  }
  if (single.size > 0) return { year, kind: 'single_age', bands: [...single.values()].sort((a, b) => a.lo - b.lo) };
  if (broad.size > 0) return { year, kind: 'broad_groups', bands: [...broad.values()].sort((a, b) => a.lo - b.lo) };
  return null;
}

// ------------------------------------------------------------------ IO price model

export const IO_INDUSTRY_PREFIX = 'industry_price_change_percent:';
export const IO_BASKET = 'household_basket_price_change_percent';
export const IO_CONTRIB = {
  domestic_products: 'household_basket_contribution_domestic_products_percent',
  imported_products: 'household_basket_contribution_imported_products_percent',
  domestic_shocks: 'household_basket_contribution_domestic_shocks_percent',
  foreign_shocks: 'household_basket_contribution_foreign_shocks_percent',
} as const;

export interface IoCell {
  value: string | null;
  missingReason: string | null;
  exogenous: string | null;
  note: string | null;
  result: SimResult;
}

export interface IoMatrix {
  countries: string[];
  industries: string[];
  cells: Map<string, IoCell>;
}

export const cellKey = (country: string, industry: string) => `${country}|${industry}`;

export function ioMatrix(results: SimResult[], variant = 'L1'): IoMatrix {
  const countries = new Set<string>();
  const industries = new Set<string>();
  const cells = new Map<string, IoCell>();
  for (const r of results) {
    if (r.variant !== variant || !r.output_key.startsWith(IO_INDUSTRY_PREFIX)) continue;
    const ind = typeof r.applicability?.industry === 'string' ? r.applicability.industry : r.output_key.slice(IO_INDUSTRY_PREFIX.length);
    const c = countryOf(r);
    countries.add(c);
    industries.add(ind);
    cells.set(cellKey(c, ind), {
      value: r.value.kind === 'decimal' ? r.value.value : null,
      missingReason: r.value.kind === 'missing' ? String(r.value.reason) : null,
      exogenous: typeof r.applicability?.exogenous === 'string' ? r.applicability.exogenous : null,
      note: typeof r.applicability?.note === 'string' ? r.applicability.note : null,
      result: r,
    });
  }
  return { countries: [...countries].sort(), industries: [...industries].sort(), cells };
}

export interface BasketRow {
  country: string;
  label: string;
  basket: SimResult | null;
  range: SimResult | null;
  contributions: Partial<Record<keyof typeof IO_CONTRIB, SimResult>>;
}

export function basketRows(results: SimResult[]): BasketRow[] {
  const by = new Map<string, BasketRow>();
  const get = (r: SimResult) => {
    const c = countryOf(r);
    let row = by.get(c);
    if (!row) {
      row = { country: c, label: r.subject?.label ?? c, basket: null, range: null, contributions: {} };
      by.set(c, row);
    }
    return row;
  };
  for (const r of results) {
    if (r.output_key === IO_BASKET) {
      if (r.variant === 'L1') get(r).basket = r;
      else if (r.variant === RANGE_VARIANT) get(r).range = r;
      continue;
    }
    if (r.variant !== 'L1') continue;
    for (const [k, key] of Object.entries(IO_CONTRIB) as [keyof typeof IO_CONTRIB, string][]) {
      if (r.output_key === key) get(r).contributions[k] = r;
    }
  }
  return [...by.values()].sort((a, b) => Number(b.basket?.value.kind === 'decimal' ? b.basket.value.value : -Infinity) - Number(a.basket?.value.kind === 'decimal' ? a.basket.value.value : -Infinity) || a.country.localeCompare(b.country));
}

// ------------------------------------------------------------------ back-test

export interface BacktestRow {
  label: string;
  predicted: string | null;
  observed: string | null;
  /** Error as served (e.g. absolute percentage error); never recomputed by the UI. */
  error: string | null;
  errorLabel: string;
  extra?: string;
}

export interface Backtest {
  title: string;
  rows: BacktestRow[];
  notes: string[];
  benchmark: string | null;
}

function s(x: unknown): string | null {
  return typeof x === 'string' ? x : typeof x === 'number' ? String(x) : null;
}

/**
 * Back-test blocks found in a run's outcome applicability (or per-country entries), in the
 * shape of reva.models.population.cohort.backtest() ({variants:{v:{groups:{g:{predicted,
 * observed, ape}}}}}) or a flat list of {label|year|age_group, predicted, observed, error}.
 */
export function findBacktests(st: SimulationStatus | null | undefined): Backtest[] {
  const out: Backtest[] = [];
  const app = isRecord(st?.outcome?.applicability) ? st!.outcome!.applicability : {};
  const candidates: [string, unknown][] = [];
  for (const k of ['backtest', 'back_test', 'validation_backtest', 'historical_validation']) if (k in app) candidates.push(['', app[k]]);
  if (isRecord(app.countries)) {
    for (const [iso, v] of Object.entries(app.countries)) {
      if (!isRecord(v)) continue;
      for (const k of ['backtest', 'back_test']) if (k in v) candidates.push([iso, v[k]]);
    }
  }
  for (const [iso, b] of candidates) out.push(...parseBacktest(b, iso));
  return out;
}

export function parseBacktest(b: unknown, prefix = ''): Backtest[] {
  if (Array.isArray(b)) return b.flatMap((x) => parseBacktest(x, prefix));
  if (!isRecord(b)) return [];
  const head = [prefix || s(b.country), b.jump_off && b.target_year ? `${s(b.jump_off)} → ${s(b.target_year)}` : null, s(b.title) ?? s(b.label)]
    .filter(Boolean)
    .join(' · ');
  const notes = [
    ...(Array.isArray(b.data_notes) ? b.data_notes.map(String) : []),
    ...(typeof b.note === 'string' ? [b.note] : []),
    ...(isRecord(b.test_period_status_flags) && Object.keys(b.test_period_status_flags).length
      ? [`test-period provider status flags: ${JSON.stringify(b.test_period_status_flags)}`]
      : []),
  ];
  const benchmark = s(b.naive_no_change_total_ape) ? `no-change benchmark: total APE ${s(b.naive_no_change_total_ape)} %` : null;
  if (isRecord(b.variants)) {
    return Object.entries(b.variants).map(([v, body]) => {
      const groups = isRecord(body) && isRecord(body.groups) ? body.groups : {};
      return {
        title: `${head}${head ? ' · ' : ''}${v}`,
        rows: Object.entries(groups).map(([g, x]) => ({
          label: g.replace(/_/g, ' '),
          predicted: isRecord(x) ? s(x.predicted) : null,
          observed: isRecord(x) ? s(x.observed) : null,
          error: isRecord(x) ? s(x.ape) : null,
          errorLabel: 'absolute % error',
          extra: isRecord(x) && s(x.mape_single_age_sex) ? `single-age×sex MAPE ${s(x.mape_single_age_sex)} %` : undefined,
        })),
        notes,
        benchmark,
      };
    });
  }
  const rowsRaw = Array.isArray(b.rows) ? b.rows : Array.isArray(b.comparison) ? b.comparison : Array.isArray(b.points) ? b.points : null;
  if (rowsRaw) {
    return [
      {
        title: head || 'Back-test',
        rows: rowsRaw.filter(isRecord).map((x) => ({
          label: [s(x.label), s(x.year), s(x.age_group), s(x.sex), s(x.output_key)].filter(Boolean).join(' · ') || '—',
          predicted: s(x.predicted),
          observed: s(x.observed),
          error: s(x.error) ?? s(x.ape) ?? s(x.abs_error) ?? s(x.error_pp),
          errorLabel: s(x.error_kind) ?? (x.ape !== undefined ? 'absolute % error' : x.error_pp !== undefined ? 'error, percentage points' : 'error'),
        })),
        notes,
        benchmark: benchmark ?? (s(b.benchmark) ? String(b.benchmark) : null),
      },
    ];
  }
  return [];
}

// ------------------------------------------------------------------ labels

const OUTPUT_LABELS: [RegExp, (m: RegExpExecArray) => string][] = [
  [/^population\.(total|female|male)\.all_ages$/, (m) => `Population, ${m[1]}, all ages (1 January)`],
  [/^population\.(total|female|male)\.age_(\d+)_(\d+)$/, (m) => `Population, ${m[1]}, ages ${m[2]}–${m[3]} (1 January)`],
  [/^population\.(total|female|male)\.age_(\d+)_plus$/, (m) => `Population, ${m[1]}, ages ${m[2]}+ (1 January)`],
  [/^old_age_dependency_ratio_(\d+)plus_per_(\d+)_(\d+)$/, (m) => `Old-age dependency ratio, persons ${m[1]}+ per 100 aged ${m[2]}–${m[3]}`],
  [/^(births|deaths|net_migration)\.(total|female|male)$/, (m) => `${m[1] === 'net_migration' ? 'Net migration (incl. statistical adjustment)' : m[1][0]!.toUpperCase() + m[1].slice(1)}, ${m[2]}, during the year`],
  [/^employ\w*55_69\w*$/, () => 'Employed persons aged 55–69'],
  [/^pension_recipients\w*$/, () => 'Pension recipients'],
  [/^pension_expenditure\w*$/, () => 'Pension expenditure change (approximate: average pension × recipients)'],
  [/^household_basket_price_change_percent$/, () => 'Household basket price change, % (basic prices)'],
];

export function multiOutputLabel(key: string): string {
  for (const [re, f] of OUTPUT_LABELS) {
    const m = re.exec(key);
    if (m) return f(m);
  }
  return key.replace(/[._]/g, ' ');
}

/** True for an output that the model itself marks approximate (or whose key says so). */
export function isApproximate(series: Series): boolean {
  if (/expenditure/.test(series.outputKey)) return true;
  return series.l1.some((p) => /approximat/i.test(JSON.stringify(p.result.applicability ?? {})));
}
