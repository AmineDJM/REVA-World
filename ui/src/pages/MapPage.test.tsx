import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter, Route, Routes } from 'react-router';
import { afterEach, describe, expect, it, vi } from 'vitest';
import MapPage from './MapPage';
import { ProvenanceProvider } from '../components/ProvenanceDrawer';
import { envelope, mockFetch } from '../test/fetchMock';

const JOB = 'job-1';

function sq(x: number, y: number) {
  return { type: 'Polygon', coordinates: [[[x, y], [x + 2, y], [x + 2, y + 2], [x, y + 2], [x, y]]] };
}

function feature(id: string, entity: string | null, name: string, x: number) {
  return { id, geometry: sq(x, 10), properties: { feature_key: id, entity_id: entity, match_status: entity ? 'reviewed_match' : 'unmatched', name, dataset_sha256: 'x' } };
}

function rel(id: string, reporter: [string, string], partner: [string, string, string], value: string, code: string) {
  return {
    id,
    predicate: 'predicate.trade_flow',
    layer: 'trade.crude_petroleum',
    support: { id, temporal: {}, spatial: { key: `COMTRADE:251->${code}` }, population: {}, aggregation: 'x', period_label: '2023' },
    component: { flow: 'M', valuation: 'CIF' },
    existence_uncertainty: {},
    value: { kind: 'decimal', value, unit_ref: 'unit.usd' },
    evidence_observation_id: `o${id}`,
    origin: 'test_fixture',
    recorded_at: '',
    participants: [
      { ordinal: 0, role: 'role.reporter', entity_id: reporter[0], entity_label: reporter[1], entity_kind: 'entity.country', direction: 'in' },
      { ordinal: 1, role: 'role.partner', entity_id: partner[0], entity_label: partner[1], entity_kind: partner[2], direction: 'out' },
    ],
  };
}

const simulation = {
  job: null,
  scenario: {
    id: 'sc-1',
    title: 'stress',
    origin: 'test_fixture',
    knowledge_cutoff: '2026-10-15T00:00:00Z',
    baseline_release: { id: 'rel', sequence: 1 },
    spec: { model_key: 'economic.crude_exposure', countries: ['FRA', 'DZA'], reference_year: 2023, fidelity: 'L1' },
  },
  run: { id: 'run', fidelity: 'L1' },
  outcome: {
    status: 'succeeded',
    applicability: {
      countries: {
        FRA: { status: 'ready', problems: [] },
        DZA: { status: 'skipped', problems: [{ input: 'M', concept: 'trade.hs2709.import_value.world_total', year: 2023, state: 'missing:unavailable' }] },
      },
    },
    problems: [],
    cost: {},
    recorded_at: '',
  },
  results: [
    {
      id: 'res-fra',
      output_key: 'exposure_percent_baseline_gdp',
      subject: { id: 'fra', label: 'France' },
      variant: 'L1',
      value: { kind: 'decimal', value: '0.2576', unit_ref: 'unit.percent_of_baseline_gdp' },
      uncertainty: { status: 'unquantified', kind: 'unknown' },
      applicability: { country: 'FRA' },
    },
  ],
};

function routes() {
  return mockFetch({
    'GET /v1/locations': { body: envelope({ features: [feature('F', 'fra', 'France', 0), feature('D', 'dza', 'Algeria', 5), feature('X', null, 'Nowhere', 9)], display_note: '', truncated: false }) },
    'GET /v1/jobs': { body: envelope([{ id: JOB, job_type: 'simulation', status: 'succeeded', payload: { scenario_id: 'sc-1', fidelity: 'L1' }, created_at: '2026-10-15T00:00:00Z' }]) },
    'GET /v1/scenarios': { body: envelope([simulation.scenario]) },
    'GET /v1/simulations/': { body: envelope(simulation, { context_mode: 'simulation', origin: 'test_fixture' }) },
    'GET /v1/entities/': (c) => (c.url.endsWith('/DZA') ? { body: envelope({ id: 'dza', label: 'Algeria' }) } : undefined),
    'GET /v1/relations': (c) =>
      c.url.includes('entity_id=fra')
        ? {
            body: envelope({
              relations: [
                rel('1', ['fra', 'France'], ['dza', 'Algeria', 'entity.country'], '3042655857.775', '12'),
                rel('2', ['fra', 'France'], ['nes', 'Areas, nes', 'entity.world_aggregate'], '1203.697', '899'),
                rel('3', ['fra', 'France'], ['kaz', 'Kazakhstan', 'entity.country'], '3603711989.944', '398'),
                rel('4', ['fra', 'France'], ['w', 'World', 'entity.world_aggregate'], '31530866245.503', '0'),
              ],
              nodes: [],
              truncated: false,
              max_edges: 500,
              depth: 1,
              matched_lower_bound: 4,
            }),
          }
        : {
            body: envelope({
              relations: [rel('5', ['fra', 'France'], ['dza', 'Algeria', 'entity.country'], '3042655857.775', '12'), rel('6', ['usa', 'United States'], ['dza', 'Algeria', 'entity.country'], '530769604', '12')],
              nodes: [],
              truncated: false,
              max_edges: 500,
              depth: 1,
              matched_lower_bound: 2,
            }),
          },
  });
}

function renderPage(path = '/map') {
  return render(
    <MemoryRouter initialEntries={[path]}>
      <ProvenanceProvider>
        <Routes>
          <Route path="/map" element={<MapPage />} />
        </Routes>
      </ProvenanceProvider>
    </MemoryRouter>,
  );
}

afterEach(() => vi.unstubAllGlobals());

describe('MapPage', () => {
  it('lists computed and hatched countries with typed reasons, never zero', async () => {
    routes();
    renderPage();
    const table = await screen.findByTestId('run-countries');
    await waitFor(() => expect(within(table).getByRole('row', { name: /Algeria/ })).toBeInTheDocument());
    const fra = within(table).getByRole('row', { name: /France/ });
    expect(fra).toHaveTextContent('+0.2576 %');
    const dza = within(table).getByRole('row', { name: /Algeria/ });
    expect(dza).toHaveTextContent('skipped: inputs incomplete');
    expect(dza).toHaveTextContent('M · trade.hs2709.import_value.world_total 2023 — missing: unavailable');
    expect(within(dza).getByText(/^missing \(/)).toBeInTheDocument();
    expect(dza).not.toHaveTextContent(/(^|\s)0 %/);
  });

  it('shows reported partners: World never a partner row, composites listed not mapped', async () => {
    routes();
    renderPage();
    const partners = await screen.findByTestId('partner-table');
    expect(within(partners).queryByText('World')).toBeNull();
    expect(within(partners).queryByText('Areas, nes')).toBeNull();
    expect(within(partners).getAllByRole('row').slice(1).map((r) => within(r).getByRole('rowheader').textContent)).toEqual(['Kazakhstan', 'Algeria']);
    expect(within(partners).getByRole('row', { name: /Algeria/ })).toHaveTextContent('arc');
    expect(within(partners).getByRole('row', { name: /Kazakhstan/ })).toHaveTextContent('listed only');
    expect(screen.getByTestId('composite-partners')).toHaveTextContent('Areas, nes');
    expect(screen.getByText(/partner-World row\(s\) excluded from arcs/)).toBeInTheDocument();
  });

  it('opens the mirror view for a country without a result, labelled "reported by partner"', async () => {
    routes();
    renderPage();
    const table = await screen.findByTestId('run-countries');
    await userEvent.click(await within(table).findByRole('button', { name: 'Algeria' }));
    const mirror = await screen.findByTestId('mirror-panel');
    const mt = await within(mirror).findByTestId('mirror-table');
    const rows = within(mt).getAllByRole('row').slice(1);
    expect(rows.map((r) => within(r).getByRole('rowheader').textContent)).toEqual(['France', 'United States']);
    for (const r of rows) expect(r).toHaveTextContent('reported by partner');
    expect(mirror).toHaveTextContent(/never.*used to fill Algeria/);
    // The country table still shows Algeria as missing: partner data never fills it.
    expect(within(table).getByRole('row', { name: /Algeria/ })).toHaveTextContent('missing');
  });
});
