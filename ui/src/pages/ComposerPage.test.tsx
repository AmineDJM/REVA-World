import { render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter, Route, Routes } from 'react-router';
import { afterEach, describe, expect, it, vi } from 'vitest';
import ComposerPage, { DRAFT_NOTICE } from './ComposerPage';
import { ProvenanceProvider } from '../components/ProvenanceDrawer';
import { envelope, mockFetch } from '../test/fetchMock';
import { READY_RESPONSE } from '../test/v2fixtures';

const REL = '7a1c2b3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d';
const release = { id: REL, sequence: 2, knowledge_cutoff: '2026-10-15T00:00:00Z', published_at: '2026-10-15T00:01:00Z', origin: 'test_fixture', method: 'm' };

function renderPage() {
  return render(
    <MemoryRouter initialEntries={['/compose']}>
      <ProvenanceProvider>
        <Routes>
          <Route path="/compose" element={<ComposerPage />} />
          <Route path="/runs/:id" element={<p>run page</p>} />
        </Routes>
      </ProvenanceProvider>
    </MemoryRouter>,
  );
}

const base = {
  'GET /v1/beliefs': { body: envelope([], { baseline_release: release, origin: 'test_fixture' }) },
  'GET /v1/belief-releases': { body: envelope([release]) },
};

afterEach(() => vi.unstubAllGlobals());

async function draft(text = 'France and USA, crude import prices +20% in 2023') {
  await userEvent.type(screen.getByLabelText(/Scenario description/), text);
  await userEvent.click(screen.getByRole('button', { name: 'Draft scenario' }));
}

describe('ComposerPage', () => {
  it('states that the draft is LLM-written and nothing runs until confirmed', () => {
    mockFetch(base);
    renderPage();
    expect(screen.getByTestId('llm-notice')).toHaveTextContent(DRAFT_NOTICE);
    expect(screen.getByRole('heading', { name: 'Describe a scenario' })).toBeInTheDocument();
  });

  it('drafts, shows restatement, gaps, numbers and model, and only Confirm creates the scenario and run', async () => {
    const { calls } = mockFetch({
      ...base,
      'POST /v1/scenario-drafts': { body: READY_RESPONSE },
      'POST /v1/scenarios': { status: 201, body: { scenario_id: 'sc-1' } },
      'POST /v1/simulations': { status: 202, body: { job_id: 'job-1', job_type: 'simulation', status: 'queued', status_url: '/v1/simulations/job-1' } },
    });
    renderPage();
    await draft();
    const review = await screen.findByTestId('draft-review');
    const draftCall = calls.find((c) => c.method === 'POST' && c.url === '/v1/scenario-drafts')!;
    expect(draftCall.body).toEqual({ text: 'France and USA, crude import prices +20% in 2023', baseline_release_id: REL });
    expect(draftCall.headers.get('Idempotency-Key')).toBeTruthy();
    // Drafting alone never creates a scenario or a run.
    expect(calls.some((c) => c.url === '/v1/scenarios' || c.url === '/v1/simulations')).toBe(false);

    expect(within(review).getByTestId('restatement')).toHaveTextContent('Crude import border price ×1.2');
    expect(within(review).getByTestId('draft-model')).toHaveTextContent('openai · gpt-4.1-2025-04-14');
    expect(within(review).getByTestId('draft-status')).toHaveAttribute('data-status', 'draft_ready');
    const gaps = within(review).getByTestId('capability-gaps');
    expect(gaps).toHaveTextContent('outputs.macroeconomic');
    expect(gaps).toHaveTextContent('“GDP loss”');
    expect(within(review).getByTestId('numbers-to-confirm')).toHaveTextContent('converted from a number you stated');

    // Edit the draft in the readable form: the diff shows the change.
    const mult = within(review).getByLabelText('import multiplier');
    await userEvent.clear(mult);
    await userEvent.type(mult, '1.3');
    expect(within(review).getByTestId('json-diff')).toHaveTextContent('intervention.import_multiplier');
    expect(within(review).getByTestId('json-diff')).toHaveTextContent('1.3');
    // Locked identity keys are not editable.
    expect(within(review).queryByLabelText('model key')).toBeNull();

    await userEvent.click(within(review).getByRole('button', { name: /Confirm and run \(L1\)/ }));
    await screen.findByText('run page');
    const sc = calls.find((c) => c.method === 'POST' && c.url === '/v1/scenarios')!;
    expect(sc.body).toMatchObject({ baseline_release_id: REL, knowledge_cutoff: '2026-10-15T00:00:00Z', intervention: { import_multiplier: 1.3 } });
    const sim = calls.find((c) => c.method === 'POST' && c.url === '/v1/simulations')!;
    expect(sim.body).toEqual({ scenario_id: 'sc-1', fidelity: 'L1' });
  });

  it('keeps Confirm disabled for a candidate spec until the user edits it', async () => {
    const { calls } = mockFetch({
      ...base,
      'POST /v1/scenario-drafts': {
        body: {
          ...READY_RESPONSE,
          status: 'needs_input',
          draft_spec: null,
          candidate_spec: { ...READY_RESPONSE.draft_spec, reference_year: null },
          missing_fields: ['reference_year'],
          validation: { valid: false, errors: [{ message: 'reference_year is required' }] },
        },
      },
    });
    renderPage();
    await draft();
    const review = await screen.findByTestId('draft-review');
    expect(within(review).getByTestId('missing-fields')).toHaveTextContent('reference_year');
    expect(within(review).getByTestId('draft-invalid')).toHaveTextContent('reference_year is required');
    const confirm = within(review).getByRole('button', { name: /Confirm and run/ });
    expect(confirm).toBeDisabled();
    // The user supplies the missing value; it stays a number and nothing else is filled in.
    const input = within(review).getByLabelText('reference_year');
    expect(input).toHaveValue('');
    await userEvent.type(input, '2023');
    expect(confirm).toBeEnabled();
    expect(within(review).getByTestId('confirm-json')).toHaveTextContent('"reference_year": 2023');
    expect(calls.some((c) => c.url === '/v1/scenarios')).toBe(false);
  });

  it('shows the typed refusal when drafting is unavailable and never posts a scenario', async () => {
    const { calls } = mockFetch({
      ...base,
      'POST /v1/scenario-drafts': {
        status: 503,
        body: { title: 'llm unavailable', status: 503, code: 'llm_unavailable', detail: 'natural-language drafting is disabled (REVA_LLM_ENABLED=false)' },
      },
    });
    renderPage();
    await draft();
    const err = await screen.findByTestId('draft-error');
    expect(err).toHaveTextContent('HTTP 503, llm_unavailable');
    expect(err).toHaveTextContent('Nothing was sent to a language model');
    expect(screen.queryByTestId('draft-review')).toBeNull();
    expect(calls.some((c) => c.url === '/v1/scenarios')).toBe(false);
  });

  it('shows an exhausted daily budget (429) as a clear message, not a fallback', async () => {
    mockFetch({
      ...base,
      'POST /v1/scenario-drafts': { status: 429, body: { title: 'budget exhausted', status: 429, code: 'budget_exhausted', detail: 'daily LLM requests used' } },
    });
    renderPage();
    await draft();
    const err = await screen.findByTestId('draft-error');
    expect(err).toHaveAttribute('data-code', 'budget_exhausted');
    expect(err).toHaveTextContent('daily drafting budget is used up');
    expect(screen.queryByTestId('draft-review')).toBeNull();
  });

  it('flags an experimental model and highlights numbers not in the text', async () => {
    mockFetch({
      ...base,
      'POST /v1/scenario-drafts': {
        body: {
          ...READY_RESPONSE,
          draft_spec: { ...READY_RESPONSE.draft_spec, model_key: 'economic.io_price' },
          warnings: ['experimental_model: economic.io_price is not yet reviewed (maintainer_review_pending)'],
          numbers_to_confirm: [{ path: 'intervention.export_multiplier', value: 1, provenance: 'not_in_text', quote: null }],
        },
      },
    });
    renderPage();
    await draft();
    const review = await screen.findByTestId('draft-review');
    expect(within(review).getByTestId('experimental-label')).toBeInTheDocument();
    const row = within(within(review).getByTestId('numbers-to-confirm')).getByRole('row', { name: /export_multiplier/ });
    expect(row).toHaveClass('number-unstated');
    expect(row).toHaveTextContent('NOT in your text');
    expect(within(review).getByText(/experimental_model: economic.io_price/)).toBeInTheDocument();
  });
});
