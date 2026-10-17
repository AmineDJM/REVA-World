import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter, Route, Routes, useLocation } from 'react-router';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { CommandPalette } from './CommandPalette';
import { envelope, mockFetch } from '../test/fetchMock';

function Where() {
  const l = useLocation();
  return <p data-testid="where">{l.pathname + l.search}</p>;
}

function renderPalette(screens = [{ to: '/world', label: 'World' }, { to: '/map', label: 'Map' }]) {
  return render(
    <MemoryRouter initialEntries={['/world']}>
      <CommandPalette screens={screens} />
      <button type="button">elsewhere</button>
      <Routes>
        <Route path="*" element={<Where />} />
      </Routes>
    </MemoryRouter>,
  );
}

function routes() {
  return mockFetch({
    'GET /v1/entities/': (c) => ({ body: envelope({ id: c.url.endsWith('FRA') ? 'fra' : `x-${c.url.slice(-3)}`, label: 'x' }) }),
    'GET /v1/entities': {
      body: envelope([
        { id: 'fra', label: 'France', kind: 'entity.country', namespace: 'reva', referent_mode: 'real', alias_count: 4 },
        { id: 'civ', label: "Côte d'Ivoire", kind: 'entity.country', namespace: 'reva', referent_mode: 'real', alias_count: 2 },
      ]),
    },
    'GET /v1/ontology': { body: envelope({ concepts: [{ concept_id: 'economics.gdp.nominal_current_usd', version: '1.0.0', labels: { en: 'GDP, current US$' } }], mappings: [] }) },
    'GET /v1/belief-releases': { body: envelope([{ id: 'rel-9', sequence: 9, knowledge_cutoff: '2026-10-15T00:00:00Z', origin: 'live' }]) },
    'GET /v1/jobs': { body: envelope([{ id: 'job-1', job_type: 'simulation', status: 'succeeded', payload: { scenario_id: 'sc-1', fidelity: 'L1' }, created_at: '2026-10-15T00:00:00Z' }]) },
    'GET /v1/scenarios': { body: envelope([{ id: 'sc-1', title: 'Cohort projection France', spec: { model_key: 'demography.cohort_component' } }]) },
  });
}

afterEach(() => vi.unstubAllGlobals());

describe('CommandPalette', () => {
  it('renders one option per screen route even when a route is listed twice (unique keys)', async () => {
    routes();
    const err = vi.spyOn(console, 'error').mockImplementation(() => {});
    renderPalette([
      { to: '/v3/relation?a=BRA&b=PRT', label: 'Pays ↔ Pays — tous les liens' },
      { to: '/map', label: 'Map' },
      { to: '/v3/relation?a=BRA&b=PRT', label: 'Pays ↔ Pays — Brésil et Portugal' },
    ]);
    await userEvent.keyboard('{Control>}k{/Control}');
    await screen.findByRole('dialog');
    await waitFor(() => expect(screen.queryByText('Loading catalogue…')).toBeNull());
    await userEvent.type(screen.getByRole('combobox'), 'relation');
    const opts = screen.getAllByRole('option').filter((o) => o.textContent?.includes('/v3/relation?a=BRA&b=PRT'));
    expect(opts).toHaveLength(1);
    expect(opts[0]).toHaveTextContent('tous les liens');
    expect(err.mock.calls.some((c) => String(c[0]).includes('same key'))).toBe(false);
    err.mockRestore();
  });

  it('opens with Ctrl+K, searches deterministically and navigates with the keyboard', async () => {
    const { calls } = routes();
    renderPalette();
    const opener = screen.getByRole('button', { name: 'elsewhere' });
    opener.focus();
    await userEvent.keyboard('{Control>}k{/Control}');
    const dialog = await screen.findByRole('dialog');
    const input = screen.getByRole('combobox');
    expect(input).toHaveFocus();
    await waitFor(() => expect(screen.queryByText('Loading catalogue…')).toBeNull());

    await userEvent.type(input, 'cote');
    expect(screen.getAllByRole('option')[0]).toHaveTextContent("Côte d'Ivoire");
    await userEvent.clear(input);
    await userEvent.type(input, 'cohort');
    const opt = screen.getAllByRole('option');
    expect(opt[0]).toHaveTextContent('Cohort projection France');
    expect(opt[0]).toHaveAttribute('aria-selected', 'true');
    expect(input).toHaveAttribute('aria-activedescendant', opt[0]!.id);
    await userEvent.keyboard('{Enter}');
    expect(screen.getByTestId('where')).toHaveTextContent('/runs/job-1');
    expect(dialog).not.toBeInTheDocument();
    // Only catalogue GETs: no POST, no LLM endpoint.
    expect(calls.every((c) => c.method === 'GET')).toBe(true);
    expect(calls.some((c) => c.url.includes('scenario-drafts'))).toBe(false);
  });

  it('moves the active option with the arrows and closes on Escape, restoring focus', async () => {
    routes();
    renderPalette();
    const opener = screen.getByRole('button', { name: 'elsewhere' });
    opener.focus();
    await userEvent.keyboard('{Meta>}k{/Meta}');
    await screen.findByRole('dialog');
    await userEvent.type(screen.getByRole('combobox'), 'fr');
    await waitFor(() => expect(screen.getAllByRole('option').length).toBeGreaterThan(0));
    await userEvent.keyboard('{ArrowDown}');
    const opts = screen.getAllByRole('option');
    if (opts.length > 1) expect(opts[1]).toHaveAttribute('aria-selected', 'true');
    await userEvent.keyboard('{Escape}');
    expect(screen.queryByRole('dialog')).toBeNull();
    await waitFor(() => expect(opener).toHaveFocus());
  });

  it('opens from the header button and lists a source that failed instead of hiding it', async () => {
    mockFetch({
      'GET /v1/entities': { status: 500, body: { title: 'boom', status: 500 } },
      'GET /v1/ontology': { body: envelope({ concepts: [], mappings: [] }) },
      'GET /v1/belief-releases': { body: envelope([]) },
      'GET /v1/jobs': { body: envelope([]) },
      'GET /v1/scenarios': { body: envelope([]) },
    });
    renderPalette();
    await userEvent.click(screen.getByRole('button', { name: /Search/ }));
    await screen.findByRole('dialog');
    expect(await screen.findByText(/Not searchable right now: countries/)).toBeInTheDocument();
    await userEvent.type(screen.getByRole('combobox'), 'map');
    expect(screen.getAllByRole('option')[0]).toHaveTextContent('Map');
  });
});
