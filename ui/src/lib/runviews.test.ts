import { describe, expect, it } from 'vitest';
import type { SimResult, SimulationStatus } from '../api/types';
import { basketRows, buildSeries, findBacktests, ioMatrix, isMultiPeriod, multiOutputLabel, parseBacktest, periodOf, pyramidFor, pyramidYears } from './runviews';

let n = 0;
function row(output: string, value: string, opts: { variant?: string; country?: string; start?: string; end?: string; app?: Record<string, unknown>; unit?: string } = {}): SimResult {
  const r: SimResult & { period_start?: string; period_end?: string } = {
    id: `r${n++}`,
    output_key: output,
    subject: { id: opts.country ?? 'FRA', label: opts.country ?? 'FRA' },
    variant: opts.variant ?? 'L1',
    value: { kind: 'decimal', value, unit_ref: opts.unit ?? 'unit.persons' },
    uncertainty: { status: 'unquantified', kind: 'unknown' },
    applicability: { country: opts.country ?? 'FRA', ...(opts.app ?? {}) },
  };
  if (opts.start) {
    r.period_start = opts.start;
    r.period_end = opts.end ?? opts.start;
  }
  return r;
}

function range(output: string, lower: string, upper: string, start: string): SimResult {
  return {
    ...row(output, '0', { start, end: start, variant: 'L2:sensitivity_range' }),
    value: { kind: 'sensitivity_range', lower, upper, unit_ref: 'unit.persons', statement: 'assumption sensitivity, not a confidence interval' },
  };
}

describe('periods', () => {
  it('reads period_start/period_end (half-open) and the {period:{start,end}} fallback', () => {
    expect(periodOf(row('x', '1', { start: '2030-01-01', end: '2030-01-02' }))).toEqual({ start: '2030-01-01', end: '2030-01-02', year: 2030 });
    const alt = { ...row('x', '1'), period: { start: '2031-01-01', end: '2032-01-01' } } as unknown as SimResult;
    expect(periodOf(alt)!.year).toBe(2031);
    expect(periodOf(row('x', '1'))).toBeNull();
    expect(isMultiPeriod([row('x', '1')])).toBe(false);
  });
});

describe('buildSeries', () => {
  it('builds the L1 path and the sensitivity range per year; grid variants are counted, not drawn', () => {
    const rs = [
      row('population.total.all_ages', '68000000', { start: '2024-01-01', end: '2024-01-02' }),
      row('population.total.all_ages', '68100000', { start: '2025-01-01', end: '2025-01-02' }),
      row('population.total.all_ages', '67000000', { start: '2025-01-01', end: '2025-01-02', variant: 'L2:mig=zero;fert=constant_base_period_rates' }),
      range('population.total.all_ages', '67000000', '68100000', '2025-01-01'),
    ];
    const [s] = buildSeries(rs);
    expect(s!.l1.map((p) => [p.year, p.value])).toEqual([
      [2024, '68000000'],
      [2025, '68100000'],
    ]);
    expect(s!.range).toHaveLength(1);
    expect(s!.range[0]).toMatchObject({ year: 2025, lower: '67000000', upper: '68100000' });
    expect(s!.variants).toEqual(['L2:mig=zero;fert=constant_base_period_rates']);
  });

  it('keeps a missing year as missing', () => {
    const m: SimResult = { ...row('x', '0', { start: '2026-01-01' }), value: { kind: 'missing', reason: 'unavailable' } };
    expect(buildSeries([m])[0]!.l1[0]).toMatchObject({ value: null, missingReason: 'unavailable' });
  });
});

describe('pyramid', () => {
  const rs = [
    row('population.female.age_000', '350000.000000001', { start: '2030-01-01', end: '2030-01-02' }),
    row('population.male.age_000', '360000', { start: '2030-01-01', end: '2030-01-02' }),
    row('population.female.age_100_plus', '25000', { start: '2030-01-01', end: '2030-01-02' }),
    row('population.female.age_000', '340000', { start: '2031-01-01', end: '2031-01-02' }),
    row('population.female.age_0_14', '5000000', { start: '2032-01-01', end: '2032-01-02' }),
    row('population.male.age_0_14', '5200000', { start: '2032-01-01', end: '2032-01-02' }),
    row('population.total.age_0_14', '10200000', { start: '2032-01-01', end: '2032-01-02' }),
  ];
  it('lists the drawable years and builds single-age bands (open group labelled)', () => {
    expect(pyramidYears(rs, 'FRA')).toEqual([2030, 2031, 2032]);
    const p = pyramidFor(rs, 'FRA', 2030)!;
    expect(p.kind).toBe('single_age');
    expect(p.bands.map((b) => [b.label, b.female, b.male])).toEqual([
      ['0', '350000.000000001', '360000'],
      ['100+', '25000', null],
    ]);
  });
  it('falls back to disjoint broad groups by sex, never the overlapping or total ones', () => {
    const p = pyramidFor(rs, 'FRA', 2032)!;
    expect(p.kind).toBe('broad_groups');
    expect(p.bands).toEqual([{ label: '0–14', lo: 0, female: '5000000', male: '5200000' }]);
    expect(pyramidFor(rs, 'FRA', 1999)).toBeNull();
  });
});

describe('IO price', () => {
  const io = (k: string, v: string, country: string, app: Record<string, unknown> = {}, variant = 'L1') => row(k, v, { country, app, unit: 'unit.percent', variant });
  const rs = [
    io('industry_price_change_percent:B', '25', 'NOR', { industry: 'B', exogenous: 'price change set by the intervention (not passed through)' }),
    io('industry_price_change_percent:C19', '3.1', 'NOR', { industry: 'C19' }),
    io('industry_price_change_percent:C19', '1.2', 'FRA', { industry: 'C19' }),
    io('industry_price_change_percent:C19', '0.6', 'FRA', { industry: 'C19' }, 'L2:pass_through=0.5'),
    io('household_basket_price_change_percent', '0.21', 'FRA'),
    io('household_basket_price_change_percent', '0.40', 'NOR'),
    io('household_basket_contribution_domestic_products_percent', '0.05', 'FRA'),
    io('household_basket_contribution_imported_products_percent', '0.16', 'FRA'),
  ];
  it('builds the country × industry matrix from L1 rows only, marking shocked cells', () => {
    const m = ioMatrix(rs);
    expect(m.countries).toEqual(['FRA', 'NOR']);
    expect(m.industries).toEqual(['B', 'C19']);
    expect(m.cells.get('FRA|C19')!.value).toBe('1.2');
    expect(m.cells.get('NOR|B')!.exogenous).toMatch(/set by the intervention/);
    expect(m.cells.get('FRA|B')).toBeUndefined();
  });
  it('lists basket rows with their decomposition, largest change first', () => {
    const b = basketRows(rs);
    expect(b.map((x) => x.country)).toEqual(['NOR', 'FRA']);
    expect(b[1]!.contributions.domestic_products!.value).toMatchObject({ value: '0.05' });
    expect(b[1]!.contributions.foreign_shocks).toBeUndefined();
  });
});

describe('back-test', () => {
  it('parses the cohort back-test shape and keeps bad errors as served', () => {
    const bt = parseBacktest({
      country: 'FRA',
      jump_off: 2013,
      target_year: 2023,
      variants: { 'mig=average_residual': { groups: { age_65_plus: { predicted: '14000000', observed: '14900000', ape: '6.040', mape_single_age_sex: '4.1' } } } },
      naive_no_change_total_ape: '3.300',
      data_notes: ['break in series 2021'],
    });
    expect(bt).toHaveLength(1);
    expect(bt[0]!.title).toContain('2013 → 2023');
    expect(bt[0]!.rows[0]).toMatchObject({ label: 'age 65 plus', predicted: '14000000', observed: '14900000', error: '6.040' });
    expect(bt[0]!.benchmark).toContain('3.300');
    expect(bt[0]!.notes).toContain('break in series 2021');
  });
  it('parses flat rows and finds back-tests in the outcome applicability', () => {
    const st = {
      results: [],
      outcome: { applicability: { backtest: { title: '2010 reform', rows: [{ year: 2019, predicted: '0.55', observed: '0.53', error_pp: '2.0' }] } } },
    } as unknown as SimulationStatus;
    const [b] = findBacktests(st);
    expect(b!.rows[0]).toMatchObject({ label: '2019', error: '2.0', errorLabel: 'error, percentage points' });
  });
});

describe('labels', () => {
  it('names outputs readably', () => {
    expect(multiOutputLabel('population.total.age_65_plus')).toBe('Population, total, ages 65+ (1 January)');
    expect(multiOutputLabel('old_age_dependency_ratio_65plus_per_15_64')).toBe('Old-age dependency ratio, persons 65+ per 100 aged 15–64');
    expect(multiOutputLabel('net_migration.female')).toMatch(/statistical adjustment/);
    expect(multiOutputLabel('pension_expenditure_change_eur')).toMatch(/approximate/);
  });
});
