import { render, screen, waitFor, within } from '@testing-library/react';
import { MemoryRouter, Route, Routes } from 'react-router';
import { afterEach, describe, expect, it, vi } from 'vitest';
import CountryPage from './CountryPage';
import { ProvenanceProvider } from '../components/ProvenanceDrawer';
import { envelope, mockFetch } from '../test/fetchMock';

const BASE = '11111111-1111-4111-8111-111111111111';
const OLD = '22222222-2222-4222-8222-222222222222';
const PROPOSAL = '33333333-3333-4333-8333-333333333333';
const M = 'trade.hs2709.import_value.world_total';

function support(year: number) {
  return { id: `s${year}`, temporal: { start: `${year}-01-01`, end: `${year + 1}-01-01`, label: String(year) }, spatial: {}, population: {}, aggregation: 'period_flow_total', period_label: String(year) };
}

function estimate(year: number, extra: Record<string, unknown> = {}) {
  return {
    id: `e-${year}`,
    release_id: BASE,
    subject: { id: 'dza', label: 'Algeria' },
    concept_id: M,
    concept_version: '1.0.0',
    support: support(year),
    modality: 'selected_report',
    value: { kind: 'decimal', value: '5564', unit_ref: 'unit.usd' },
    uncertainty: { status: 'unquantified', kind: 'unknown' },
    alternatives: [],
    discrepancy_flags: [],
    source_key: 'un.comtrade',
    observation_id: `o-${year}`,
    method: { id: 'm', model_key: 'source_select_v1', version: '1.0.0' },
    ...extra,
  };
}

const release = { id: BASE, sequence: 7, label: 'r7', event_type: 'release', support: support(2026), component: {}, origin: 'test_fixture', knowledge_cutoff: '2026-10-15T00:00:00Z' };

function renderAt(path: string) {
  return render(
    <MemoryRouter initialEntries={[path]}>
      <ProvenanceProvider>
        <Routes>
          <Route path="/country/:iso3" element={<CountryPage />} />
        </Routes>
      </ProvenanceProvider>
    </MemoryRouter>,
  );
}

afterEach(() => vi.unstubAllGlobals());

describe('CountryPage source flags and staleness (APIX)', () => {
  it('reads provider flags from /v1/beliefs, marks stale rows and earlier-release rows, and never calls /v1/observations', async () => {
    const { calls } = mockFetch({
      'GET /v1/entities/': { body: envelope({ id: 'dza', label: 'Algeria', kind: 'entity.country', namespace: 'reva', referent_mode: 'real', recorded_at: '2026-10-15T00:00:00Z', aliases: [], components: [], supported_contexts: [] }, { origin: 'test_fixture' }) },
      'GET /v1/ontology': { body: envelope({ concepts: [], mappings: [] }) },
      'GET /v1/locations': { body: envelope({ features: [], display_note: '', truncated: false }) },
      'GET /v1/coverage': { body: envelope({ statement: '', catalogue_version: '1', totals: {}, requirements: [] }) },
      'GET /v1/gaps': { body: envelope([]) },
      'GET /v1/beliefs': (c) =>
        c.url.includes(encodeURIComponent(M)) || c.url.includes(M)
          ? {
              body: envelope(
                [
                  estimate(2023, {
                    source_flags: { isReported: false, legacyEstimationFlag: 6, provider_value_estimated: null },
                    source_flags_truncated: true,
                  }),
                  estimate(2024, {
                    stale: true,
                    stale_reason: 'source_schema_quarantine',
                    proposal_id: PROPOSAL,
                    served_from_release_id: OLD,
                    source_flags: { provider_value_estimated: true },
                  }),
                ],
                {
                  baseline_release: release,
                  origin: 'test_fixture',
                  warnings: [{ code: 'publication_paused', message: `${M} (source_schema_quarantine): withheld from this release; serving release #6 as stale` }],
                },
              ),
            }
          : { body: envelope([], { baseline_release: release }) },
      'GET /v1/belief-releases/': { body: envelope({ id: OLD, sequence: 6 }) },
    });
    renderAt('/country/DZA');
    const panel = (await screen.findByRole('heading', { name: /Crude petroleum imports/, level: 2 })).closest('section')!;
    const row2023 = await within(panel).findByRole('row', { name: /^2023/ });
    expect(within(row2023).getByTestId('source-flag')).toHaveTextContent('estimation status undeterminable from provider flags');
    expect(within(row2023).getByTestId('source-flag')).toHaveTextContent('not directly reported (isReported=false)');
    expect(within(row2023).getByTestId('flags-truncated')).toBeInTheDocument();
    expect(within(row2023).queryByTestId('stale-notes')).toBeNull();

    const row2024 = within(panel).getByRole('row', { name: /^2024/ });
    const notes = within(row2024).getByTestId('stale-notes');
    expect(notes).toHaveTextContent('stale: source schema quarantine');
    await waitFor(() => expect(notes).toHaveTextContent('served from release #6'));
    expect(within(notes).getByRole('link', { name: 'repair proposal' })).toHaveAttribute('href', expect.stringContaining(PROPOSAL));
    expect(within(row2024).getByTestId('source-flag')).toHaveTextContent('provider estimate');

    expect(within(panel).getByTestId('publication-paused')).toHaveTextContent('PUBLICATION PAUSED');
    expect(calls.some((c) => c.url.startsWith('/v1/observations'))).toBe(false);
  });
});
