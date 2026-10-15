import { fireEvent, render, screen, within } from '@testing-library/react';
import { MemoryRouter } from 'react-router';
import { describe, expect, it } from 'vitest';
import { RunBody } from './RunPage';
import { ProvenanceProvider } from '../components/ProvenanceDrawer';
import { envelope } from '../test/fetchMock';
import { EXPERIMENTAL_LABEL, IO_LABEL, PROJECTION_LABEL } from '../components/HonestyLabels';
import type { Envelope, SimResult, SimulationStatus } from '../api/types';

let n = 0;
function r(output: string, value: string, opts: { variant?: string; country?: string; year?: number; flow?: boolean; unit?: string; app?: Record<string, unknown> } = {}): SimResult {
  const y = opts.year;
  const out: Record<string, unknown> = {
    id: `r${n++}`,
    output_key: output,
    subject: { id: opts.country ?? 'FRA', label: opts.country === 'NOR' ? 'Norway' : 'France' },
    variant: opts.variant ?? 'L1',
    value: { kind: 'decimal', value, unit_ref: opts.unit ?? 'unit.persons' },
    uncertainty: { status: 'unquantified', kind: 'unknown' },
    applicability: { country: opts.country ?? 'FRA', ...(opts.app ?? {}) },
  };
  if (y !== undefined) {
    out.period_start = `${y}-01-01`;
    out.period_end = opts.flow ? `${y + 1}-01-01` : `${y}-01-02`;
  }
  return out as unknown as SimResult;
}

function env(modelKey: string, results: SimResult[], extra: Partial<SimulationStatus> = {}, fidelity = 'L1'): Envelope<SimulationStatus> {
  return envelope(
    {
      job: null,
      scenario: { id: 'sc', context_id: 'c', origin: 'test_fixture', title: 't', baseline_release: null, spec: { model_key: modelKey, fidelity }, spec_digest: '', spec_sha256: '', created_by: '', recorded_at: '' },
      run: { id: 'run', fidelity, model_manifest_sha256: '', ontology_manifest_sha256: '', seed_plan: {}, execution_profile: {}, recorded_at: '' },
      outcome: { status: 'succeeded', applicability: { countries: { FRA: { status: 'ready', problems: [] } } }, problems: [], cost: {}, recorded_at: '' },
      results,
      ...extra,
    },
    { context_mode: 'simulation', modality: 'assumption_conditioned_scenario', model_versions: [{ id: 'm', model_key: modelKey, version: '1.0.0', approval_status: 'experimental' }] },
  ) as unknown as Envelope<SimulationStatus>;
}

function renderBody(e: Envelope<SimulationStatus>) {
  return render(
    <MemoryRouter>
      <ProvenanceProvider>
        <RunBody id="job-1" env={e} />
      </ProvenanceProvider>
    </MemoryRouter>,
  );
}

describe('RunPage: cohort-component projection', () => {
  const results = [
    ...[2023, 2024, 2025].flatMap((y, i) => [
      r('population.total.all_ages', String(68000000 + i * 100000), { year: y }),
      r('population.female.age_000', String(350000 - i * 1000), { year: y }),
      r('population.male.age_000', String(360000 - i * 1000), { year: y }),
      r('population.female.age_100_plus', '30000', { year: y }),
      r('population.male.age_100_plus', '9000', { year: y }),
    ]),
    { ...r('population.total.all_ages', '0', { year: 2025, variant: 'L2:sensitivity_range' }), value: { kind: 'sensitivity_range', lower: '67900000', upper: '68200000', unit_ref: 'unit.persons', statement: 'assumption sensitivity, not a confidence interval' } } as SimResult,
  ];

  it('labels the projection, the experimental model, and draws a pyramid with a year slider', () => {
    renderBody(env('demography.cohort_component', results, {}, 'L2'));
    expect(screen.getByTestId('projection-label')).toHaveTextContent(PROJECTION_LABEL);
    expect(screen.getByTestId('experimental-label')).toHaveTextContent(EXPERIMENTAL_LABEL);
    const slider = screen.getByLabelText('Year (1 January)');
    expect(screen.getByTestId('pyramid-year')).toHaveTextContent('2023');
    fireEvent.change(slider, { target: { value: '2' } });
    expect(screen.getByTestId('pyramid-year')).toHaveTextContent('2025');
    expect(slider).toHaveAttribute('aria-valuetext', '2025');
    expect(screen.getByRole('img', { name: /Population pyramid of FRA on 1 January 2025, single ages/ })).toBeInTheDocument();
    // Pyramid table alternative: 100+ open group, whole persons.
    const table = screen.getByRole('table', { name: /Population of FRA on 1 January 2025/ });
    expect(within(table).getByRole('rowheader', { name: '100+' })).toBeInTheDocument();
    expect(within(table).getByRole('row', { name: /^0 / })).toHaveTextContent('358,000');
  });

  it('charts indicators with period bounds and the sensitivity ribbon, and shows the back-test panel even when absent', () => {
    renderBody(env('demography.cohort_component', results, {}, 'L2'));
    expect(screen.getByTestId('sensitivity-ribbon')).toBeInTheDocument();
    const st = screen.getByRole('table', { name: /Population, total, all ages/ });
    expect(within(st).getByRole('rowheader', { name: /2025 \[2025-01-01, 2025-01-02\)/ })).toBeInTheDocument();
    expect(within(st).getByRole('row', { name: /^2025/ })).toHaveTextContent('67,900,000 to 68,200,000');
    expect(within(st).getByRole('row', { name: /^2025/ })).toHaveTextContent('L1 at upper end');
    expect(screen.getByTestId('backtest-absent')).toHaveTextContent('docs/dev/v2_pop_backtest.md');
  });
});

describe('RunPage: retirement age (generic multi-period)', () => {
  it('shows each indicator, marks expenditure approximate and shows bad back-test errors as served', () => {
    const results = [2020, 2021].flatMap((y) => [
      r('employed_55_69', String(5000000 + y), { year: y, flow: true }),
      r('pension_expenditure_change_eur', '1200000000', { year: y, flow: true, unit: 'unit.eur' }),
    ]);
    const e = env('social.retirement_age', results, {
      outcome: {
        status: 'succeeded',
        applicability: { backtest: { title: '2010 reform, employment rate 60–64', rows: [{ year: 2019, predicted: '0.62', observed: '0.53', error_pp: '9.0' }] } },
        problems: [],
        cost: {},
        recorded_at: '',
      },
    });
    renderBody(e);
    expect(screen.getByTestId('projection-label')).toBeInTheDocument();
    expect(screen.getByTestId('experimental-label')).toBeInTheDocument();
    expect(document.querySelector('figure[data-output-key="employed_55_69"]')).toHaveTextContent('Employed persons aged 55–69');
    const exp = document.querySelector('figure[data-output-key="pension_expenditure_change_eur"]') as HTMLElement;
    expect(within(exp).getByText('approximate')).toBeInTheDocument();
    const bt = screen.getByTestId('backtest-panel');
    expect(within(bt).getByRole('row', { name: /2019/ })).toHaveTextContent('9.0');
    expect(within(bt).getByRole('columnheader', { name: 'error, percentage points' })).toBeInTheDocument();
  });
});

describe('RunPage: truncated results', () => {
  it('says when the API returned only part of the rows', () => {
    const e = env('economic.io_price', [r('household_basket_price_change_percent', '0.2', { unit: 'unit.percent' })]);
    e.warnings = [{ code: 'results_truncated', message: 'Only the first 5000 of 17000 result rows are returned.' }];
    renderBody(e);
    expect(screen.getByTestId('results-truncated')).toHaveTextContent('Only the first 5000 of 17000');
    expect(screen.getByTestId('results-truncated')).toHaveTextContent('not zero');
  });
});

describe('RunPage: IO price propagation', () => {
  it('labels first-round cost-push, shows the heatmap, basket and decomposition', () => {
    const io = (k: string, v: string, country: string, app: Record<string, unknown> = {}) => r(k, v, { country, app, unit: 'unit.percent' });
    const results = [
      io('industry_price_change_percent:B', '25', 'NOR', { industry: 'B', exogenous: 'price change set by the intervention (not passed through)' }),
      io('industry_price_change_percent:C19', '3.1', 'NOR', { industry: 'C19' }),
      io('industry_price_change_percent:B', '0.4', 'FRA', { industry: 'B' }),
      io('industry_price_change_percent:C19', '1.25', 'FRA', { industry: 'C19' }),
      io('household_basket_price_change_percent', '0.21', 'FRA'),
      io('household_basket_contribution_domestic_products_percent', '0.05', 'FRA'),
      io('household_basket_contribution_imported_products_percent', '0.16', 'FRA'),
      io('household_basket_contribution_domestic_shocks_percent', '0', 'FRA'),
      io('household_basket_contribution_foreign_shocks_percent', '0.21', 'FRA'),
    ];
    renderBody(env('economic.io_price', results));
    expect(screen.getByTestId('io-label')).toHaveTextContent(IO_LABEL);
    expect(screen.queryByTestId('projection-label')).toBeNull();
    const hm = screen.getByTestId('io-heatmap');
    expect(within(hm).getAllByRole('row')).toHaveLength(3);
    expect(within(hm).getByText('C19: 1.25 %')).toBeInTheDocument();
    expect(within(hm).getByText(/B: 25 %.*\(shocked\)/)).toBeInTheDocument();
    const basket = screen.getByTestId('basket-table');
    const fra = within(basket).getByRole('row', { name: /France/ });
    expect(fra).toHaveTextContent('0.21');
    expect(fra).toHaveTextContent('0.05');
    expect(fra).toHaveTextContent('0.16');
  });
});
