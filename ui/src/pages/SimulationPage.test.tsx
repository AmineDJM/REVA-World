import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter } from 'react-router';
import { afterEach, describe, expect, it, vi } from 'vitest';
import SimulationPage, { ResultsView } from './SimulationPage';
import { ProvenanceProvider } from '../components/ProvenanceDrawer';
import { envelope, mockFetch } from '../test/fetchMock';
import { FIXTURE_BASELINE_UUID } from '../lib/scenario';

const REAL = '7a1c2b3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d';

function release(id: string, origin: 'live' | 'test_fixture' = 'live') {
  return { id, sequence: 4, knowledge_cutoff: '2026-10-14T20:00:00Z', published_at: '2026-10-14T20:05:00Z', origin, method: 'source_select_v1@1.0.0' };
}

function renderPage() {
  return render(
    <MemoryRouter initialEntries={['/simulation']}>
      <ProvenanceProvider>
        <SimulationPage />
      </ProvenanceProvider>
    </MemoryRouter>,
  );
}

afterEach(() => vi.unstubAllGlobals());

describe('SimulationPage', () => {
  it('shows the persistent SIMULATION banner', async () => {
    mockFetch({
      'GET /v1/beliefs': { body: envelope([], { baseline_release: release(REAL) }) },
      'GET /v1/belief-releases': { body: envelope([]) },
    });
    renderPage();
    expect(screen.getByTestId('simulation-screen-banner')).toHaveTextContent(/^SIMULATION/);
    expect(screen.getByTestId('simulation-banner')).toBeInTheDocument();
  });

  it('rejects the fixture baseline UUID and never submits a scenario', async () => {
    const { calls } = mockFetch({
      'GET /v1/beliefs': { body: envelope([], { baseline_release: release(FIXTURE_BASELINE_UUID, 'test_fixture') }) },
      'GET /v1/belief-releases': { body: envelope([]) },
    });
    renderPage();
    const err = await screen.findByTestId('baseline-error');
    expect(err).toHaveTextContent(/fixture baseline UUID is not a real release/i);
    const submit = screen.getByRole('button', { name: /create scenario and run/i });
    expect(submit).toBeDisabled();
    await userEvent.click(submit);
    expect(calls.some((c) => c.method === 'POST')).toBe(false);
  });

  it('blocks submission when no release is published', async () => {
    mockFetch({
      'GET /v1/beliefs': { status: 404, body: { title: 'no release', status: 404, detail: 'no belief release has been published', code: 'no_release' } },
      'GET /v1/belief-releases': { body: envelope([]) },
    });
    renderPage();
    expect(await screen.findByTestId('baseline-error')).toHaveTextContent(/No published baseline release/);
    expect(screen.getByRole('button', { name: /create scenario and run/i })).toBeDisabled();
  });

  it('submits a scenario pinned to the real release with idempotency keys and polls the job', async () => {
    const SCN = '0b9e4a52-6f0a-4c1d-9a39-1b2c3d4e5f60';
    const JOB = '5d4c3b2a-1f0e-4d9c-8b7a-6f5e4d3c2b1a';
    const { calls } = mockFetch({
      'GET /v1/beliefs': { body: envelope([], { baseline_release: release(REAL) }) },
      'GET /v1/belief-releases': { body: envelope([release(REAL)]) },
      'POST /v1/scenarios': { status: 201, body: { scenario_id: SCN, context_id: 'c', spec_digest: 'd', spec_sha256: 's', baseline_release_id: REAL, knowledge_cutoff: '2026-10-14T20:00:00Z', created_by: 'human:admin', recorded_at: '2026-10-14T21:00:00Z' } },
      'POST /v1/simulations': { status: 202, body: { job_id: JOB, job_type: 'simulation', status: 'queued', status_url: `/v1/simulations/${JOB}` } },
      'GET /v1/jobs/': {
        body: { id: JOB, job_type: 'simulation', status: 'running', payload: {}, priority: 0, attempts: 1, max_attempts: 3, progress: {}, has_checkpoint: false, created_by: 'human:admin', created_at: '', updated_at: '', available_at: '', cancellable: true, links: {} },
      },
    });
    renderPage();
    const submit = await screen.findByRole('button', { name: /create scenario and run/i });
    await waitFor(() => expect(submit).toBeEnabled());
    await userEvent.click(submit);

    await waitFor(() => expect(calls.some((c) => c.method === 'POST' && c.url === '/v1/simulations')).toBe(true));
    const scenarioCall = calls.find((c) => c.method === 'POST' && c.url === '/v1/scenarios')!;
    const spec = scenarioCall.body as Record<string, unknown>;
    expect(spec.baseline_release_id).toBe(REAL);
    expect(spec.baseline_release_id).not.toBe(FIXTURE_BASELINE_UUID);
    expect(spec.knowledge_cutoff).toBe('2026-10-14T20:00:00Z');
    expect(scenarioCall.headers.get('Idempotency-Key')).toBeTruthy();
    const simCall = calls.find((c) => c.method === 'POST' && c.url === '/v1/simulations')!;
    expect(simCall.body).toEqual({ scenario_id: SCN, fidelity: 'L1' });
    expect(simCall.headers.get('Idempotency-Key')).toBeTruthy();
    expect(simCall.headers.get('Idempotency-Key')).not.toBe(scenarioCall.headers.get('Idempotency-Key'));

    expect(await screen.findByRole('button', { name: /cancel run/i })).toBeInTheDocument();
    expect(screen.getByText(/running/i)).toBeInTheDocument();
  });

  it('shows a missing_input rejection (e.g. Algeria) as problem details', async () => {
    mockFetch({
      'GET /v1/beliefs': { body: envelope([], { baseline_release: release(REAL) }) },
      'GET /v1/belief-releases': { body: envelope([]) },
      'POST /v1/scenarios': { status: 201, body: { scenario_id: 's1' } },
      'POST /v1/simulations': {
        status: 422,
        body: {
          type: 'x', title: 'missing input', status: 422, detail: 'required inputs are missing from the pinned baseline release', code: 'missing_input',
          details: { missing: [{ country: 'DZA', input: 'M', concept: 'trade.hs2709.import_value.world_total', year: 2023, state: 'missing:unavailable' }] },
        },
      },
    });
    renderPage();
    await userEvent.click(await screen.findByRole('checkbox', { name: 'DZA' }));
    expect(screen.getByRole('note')).toHaveTextContent(/never dropped silently or treated as zero/);
    const submit = screen.getByRole('button', { name: /create scenario and run/i });
    await waitFor(() => expect(submit).toBeEnabled());
    await userEvent.click(submit);
    expect(await screen.findByText(/required inputs are missing/)).toBeInTheDocument();
    expect(screen.getByText(/HTTP 422, missing_input/)).toBeInTheDocument();
    // Listed as a data gap per input, never as zero.
    const gaps = screen.getByTestId('missing-inputs');
    expect(within(gaps).getByRole('table')).toHaveAccessibleName(/missing for DZA — a data gap, not zero/);
    expect(within(gaps).getByRole('rowheader', { name: 'DZA' })).toBeInTheDocument();
    expect(within(gaps).getByText('missing (unavailable)')).toBeInTheDocument();
    expect(within(gaps).queryByText(/^0$/)).toBeNull();
  });

  it('states the scenario limits, uncertainty and provider flags on baseline inputs', () => {
    const fr = { id: 'e1', label: 'France' };
    const notClaimed = 'It is not a GDP loss, welfare change or forecast.';
    const results = [
      {
        id: 'a', output_key: 'import_value_change_usd', subject: fr, variant: 'L1',
        value: { kind: 'decimal', value: '250', unit_ref: 'unit.usd', basis: {} }, uncertainty: { status: 'unquantified', kind: 'unknown' },
        applicability: {
          inputs: { M: { value: '1000', valuation: 'CIF' }, X: { value: '10', valuation: 'FOB' }, G: { value: '5000' } },
          source_flags: { M: { note: 'provider estimation flagged (legacyEstimationFlag != 0): not a direct customs observation', provider_estimation_flagged: true } },
        },
      },
    ];
    const outcome = {
      status: 'succeeded', applicability: { explanation: { not_claimed: notClaimed, uncertainty: 'Unquantified: no error model.', sign_convention: 'Positive means higher net payment.' } },
      problems: [], cost: {}, recorded_at: '',
    };
    const env = envelope(
      { job: null, scenario: null, run: { id: 'run', fidelity: 'L1', model_manifest_sha256: 'm', ontology_manifest_sha256: 'o', seed_plan: {}, execution_profile: {}, recorded_at: '' }, outcome, results },
      { context_mode: 'simulation', modality: 'assumption_conditioned_scenario' },
    );
    render(
      <MemoryRouter>
        <ProvenanceProvider>
          <ResultsView simId="sim" env={env as never} />
        </ProvenanceProvider>
      </MemoryRouter>,
    );
    const limits = screen.getByTestId('result-limits');
    expect(limits).toHaveTextContent(notClaimed);
    expect(limits).toHaveTextContent('Unquantified: no error model.');
    expect(limits).toHaveTextContent('Positive means higher net payment.');
    expect(screen.getByText(/not a direct customs observation/)).toBeInTheDocument();
  });

  it('renders the L2 sensitivity-range row in the range column, not as a grid variant', () => {
    const fr = { id: 'e1', label: 'France' };
    const usd = { unit_ref: 'unit.usd', basis: {} };
    const results = [
      { id: 'a', output_key: 'import_value_change_usd', subject: fr, variant: 'L1', value: { kind: 'decimal', value: '250', ...usd }, uncertainty: { status: 'unquantified', kind: 'unknown' }, applicability: {} },
      { id: 'b', output_key: 'import_value_change_usd', subject: fr, variant: 'L2:eM=-0.5;eX=-0.5', value: { kind: 'decimal', value: '400', ...usd }, uncertainty: { status: 'unquantified', kind: 'unknown' }, applicability: {} },
      {
        id: 'c', output_key: 'import_value_change_usd', subject: fr, variant: 'L2:sensitivity_range',
        value: { kind: 'sensitivity_range', lower: '250', upper: '562.5', evaluated_points: 3, includes_fixed_quantity_point: true, statement: 'assumption sensitivity, not a confidence interval', ...usd },
        uncertainty: { status: 'quantified', kind: 'sensitivity_range', lower: '250', upper: '562.5' }, applicability: {},
      },
    ];
    const env = envelope(
      { job: null, scenario: null, run: { id: 'run', fidelity: 'L2', model_manifest_sha256: 'm', ontology_manifest_sha256: 'o', seed_plan: {}, execution_profile: {}, recorded_at: '' }, outcome: null, results },
      { context_mode: 'simulation', modality: 'assumption_conditioned_scenario' },
    );
    render(
      <MemoryRouter>
        <ProvenanceProvider>
          <ResultsView simId="sim" env={env as never} />
        </ProvenanceProvider>
      </MemoryRouter>,
    );
    const table = screen.getByTestId('comparison-table');
    const range = within(table).getByTitle('assumption sensitivity, not a confidence interval');
    expect(range.textContent).toMatch(/250.*to.*562\.5/);
    expect(range.textContent).toContain('not a confidence interval');
    expect(screen.getByText(/L2 grid variants \(1 assumed elasticity pairs\)/)).toBeInTheDocument();
  });
});
