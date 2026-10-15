import { configure, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter, Route, Routes } from 'react-router';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { mockFetch } from '../../test/fetchMock';
import * as F from '../test/fixtures';
import V3App from '../V3App';

// Canvas/WebGL libraries are not exercised in jsdom; their honest text alternatives are.
vi.mock('../components/NeuronGraph', () => ({
  default: ({ label, onTapNode }: { label: string; onTapNode: (id: string) => void }) => (
    <div role="img" aria-label={label} data-testid="neuron-canvas">
      <button type="button" onClick={() => onTapNode('Q5146')}>
        tap-Q5146
      </button>
    </div>
  ),
}));
vi.mock('../components/AtlasMap', () => ({ default: ({ label }: { label: string }) => <div role="img" aria-label={label} data-testid="atlas-map" /> }));

// Lazy screens + chained requests: allow for a loaded CI machine.
configure({ asyncUtilTimeout: 5000 });
vi.setConfig({ testTimeout: 20_000 });

afterEach(() => vi.unstubAllGlobals());

function at(path: string) {
  return render(
    <MemoryRouter initialEntries={[path]}>
      <Routes>
        <Route path="/v3/*" element={<V3App />} />
      </Routes>
    </MemoryRouter>,
  );
}

const KG_ROUTES = {
  'GET /v1/kg/entities/Q155/neighbours': { body: F.NEIGHBOURS_Q155 },
  'GET /v1/kg/entities/Q5146/neighbours': { body: F.NEIGHBOURS_Q5146 },
  'GET /v1/kg/entities/Q155/timeline': { body: F.TIMELINE_Q155 },
  'GET /v1/kg/entities/': (c: { url: string }) => {
    const id = decodeURIComponent(c.url.split('/v1/kg/entities/')[1]!.split(/[/?]/)[0]!);
    const e = F.ENTITIES[id];
    return e ? { body: F.envelope(e) } : { status: 404, body: { title: 'Not Found', status: 404, detail: 'entity not found in the knowledge graph', code: 'not_found' } };
  },
  'GET /v1/kg/search': { body: F.SEARCH_BRESIL },
};

describe('Neurones', () => {
  it('starts from an entity, shows its card with provenance and every statement with its citation', async () => {
    mockFetch(KG_ROUTES);
    at('/v3/neurones?qid=Q155');
    const card = await screen.findByTestId('entity-card', {}, { timeout: 5000 });
    expect(within(card).getByRole('heading', { name: /Brésil/ })).toBeInTheDocument();
    expect(within(card).getByRole('link', { name: 'Q155' })).toHaveAttribute('href', 'https://www.wikidata.org/wiki/Q155');
    expect(within(card).getByTestId('community-badge')).toHaveTextContent('communautaire, non vérifié');
    await waitFor(() => expect(screen.getAllByTestId('citation').length).toBe(4), { timeout: 5000 });
    const first = screen.getAllByTestId('citation')[0]!;
    expect(within(first).getAllByRole('link')[0]).toHaveAttribute('href', expect.stringMatching(/^https:\/\/www\.wikidata\.org\/wiki\/Q\d+#Q\d+\$FIXTURE-/));
    expect(within(first).getByRole('link', { name: 'révision' })).toHaveAttribute('href', 'https://www.wikidata.org/w/index.php?oldid=2250000000');
    expect(screen.getAllByText(/non daté dans la source/, { selector: '.v3-undated' }).length).toBeGreaterThan(0);
    expect(screen.getByTestId('neuron-canvas')).toHaveAttribute('aria-label', expect.stringMatching(/5 entités et 4 déclarations/));
  });

  it('clicking a node selects and expands it', async () => {
    const { calls } = mockFetch(KG_ROUTES);
    at('/v3/neurones?qid=Q155');
    await screen.findByTestId('entity-card', {}, { timeout: 5000 });
    await userEvent.click(await screen.findByRole('button', { name: 'tap-Q5146' }, { timeout: 5000 }));
    await waitFor(() => expect(calls.some((c) => c.url.startsWith('/v1/kg/entities/Q5146/neighbours'))).toBe(true), { timeout: 5000 });
    expect(await screen.findByRole('heading', { name: /portugais/ }, { timeout: 5000 })).toBeInTheDocument();
  });

  it('says "pas encore disponible" when the KG API is not deployed, and shows no graph', async () => {
    mockFetch({ 'GET /v1/kg/': { status: 404, body: F.NOT_FOUND_ROUTE } });
    at('/v3/neurones?qid=Q155');
    expect(await screen.findByText('Pas encore disponible')).toBeInTheDocument();
    expect(screen.queryByTestId('neuron-canvas')).toBeNull();
  });
});

describe('Chemin', () => {
  it('shows the path-is-not-a-cause banner, "relié via" hops with citations, causal wording only where asserted', async () => {
    mockFetch({ ...KG_ROUTES, 'GET /v1/links/path': { body: F.LINKS_PATH_COL_BRA } });
    at('/v3/chemin?from=Q7322&to=Q155');
    expect(await screen.findByTestId('path-not-cause')).toHaveTextContent('Un chemin n’est pas une cause.');
    const chains = await screen.findAllByTestId('path-chain');
    expect(chains).toHaveLength(2);
    const hops0 = within(chains[0]!).getAllByTestId('path-hop');
    expect(hops0[0]).toHaveTextContent('relié via « participant à »');
    for (const h of hops0) {
      expect(within(h).queryByTestId('causal-wording')).toBeNull();
      expect(within(h).getAllByTestId('citation').length).toBeGreaterThan(0);
    }
    const causal = within(chains[1]!).getAllByTestId('causal-wording');
    expect(causal).toHaveLength(1);
    expect(causal[0]).toHaveTextContent(/^selon Wikidata/);
    expect(causal[0]).toHaveTextContent('non établie par REVA');
  });

  it('falls back to the raw KG path when the links engine is not deployed', async () => {
    mockFetch({ ...KG_ROUTES, 'GET /v1/links/path': { status: 404, body: F.NOT_FOUND_ROUTE }, 'GET /v1/kg/path': { body: F.KG_PATH_COL_BRA } });
    at('/v3/chemin?from=Q7322&to=Q155');
    expect(await screen.findByText(/le moteur de liens typés n’est pas encore disponible/)).toBeInTheDocument();
    expect(screen.getAllByTestId('path-hop')).toHaveLength(2);
  });

  it('no path is not "no connection"', async () => {
    mockFetch({ ...KG_ROUTES, 'GET /v1/links/path': { body: F.LINKS_PATH_NONE } });
    at('/v3/chemin?from=Q7322&to=Q155');
    expect(await screen.findByText(/cela ne prouve pas l’absence de lien/)).toBeInTheDocument();
  });
});

describe('Atlas', () => {
  it('renders gaps as "manquant" with a reason (never 0), the legend with sources and the ranking', async () => {
    mockFetch({
      'GET /v1/atlas/concepts': { body: F.CONCEPTS },
      'GET /v1/atlas/coverage': { body: F.COVERAGE_UNEMP },
      'GET /v1/atlas/map': (c) => ({ body: F.mapResponse(Number(new URL(c.url, 'http://x').searchParams.get('year'))) }),
      'GET /v1/locations': { body: F.LOCATIONS },
    });
    at('/v3/atlas?concept=labour.unemployment_rate');
    const legend = await screen.findByTestId('atlas-legend');
    expect(legend).toHaveTextContent('manquant — 3 pays');
    expect(legend).toHaveTextContent('ilo.ilostat_une.UNE_DEAP_SEX_AGE_RT : 4 pays');
    expect(legend).toHaveTextContent('donnée de test');
    const ranking = screen.getByTestId('atlas-ranking');
    const rows = within(ranking).getAllByRole('row');
    expect(rows[0]).toHaveAttribute('data-iso3', 'ZAF');
    expect(rows[0]).toHaveTextContent('29,8 %');
    const gaps = screen.getByTestId('atlas-gaps');
    for (const iso of ['PRK', 'ERI', 'SOM']) {
      const li = gaps.querySelector(`[data-gap-country="${iso}"]`)!;
      expect(li).toHaveTextContent(/manquant \(aucune source déclarée/);
      expect(li.textContent).not.toMatch(/\b0\b/);
    }
    expect(await screen.findByTestId('atlas-map')).toHaveAttribute('aria-label', expect.stringContaining('3 sans valeur (hachurés)'));
  });
});

describe('Dossier pays', () => {
  it('shows each value with source and year, missing cells with the sources searched, links and history', async () => {
    mockFetch({
      'GET /v1/atlas/countries/FRA': { body: F.DOSSIER_FRA },
      'GET /v1/atlas/concepts': { body: F.CONCEPTS },
      'GET /v1/links/FRA': { body: F.LINKS_FRA },
      'GET /v1/kg/entities/Q142/timeline': { body: F.TIMELINE_Q142 },
      'GET /v1/locations': { body: F.LOCATIONS },
    });
    at('/v3/pays/FRA');
    expect(await screen.findByRole('heading', { level: 1, name: /France/ })).toBeInTheDocument();
    const row = document.querySelector('[data-concept="labour.unemployment_rate"]')!;
    expect(row).toHaveTextContent('7,3 %');
    expect(row).toHaveTextContent('2022');
    expect(row).toHaveTextContent('ilo/ilostat_une');
    expect(document.querySelector('[data-concept="law.legal_system"]')).toHaveTextContent('droit civil');
    const missing = screen.getByTestId('missing-labour');
    expect(missing).toHaveTextContent('Salaire minimum mensuel');
    expect(missing).toHaveTextContent('manquant');
    expect(missing).toHaveTextContent('sources consultées : ilo.ilostat_mw.EAR_INEE_NOC_NB');
    expect(await screen.findByText('Espagne')).toBeInTheDocument();
    expect(await screen.findByText(/Ouvrir la frise complète/)).toBeInTheDocument();
  });

  it('a dossier with no atlas snapshot says "aucune donnée chargée"', async () => {
    mockFetch({
      'GET /v1/atlas/countries/FRA': { status: 404, body: { title: 'Not Found', status: 404, detail: 'no atlas snapshot recorded', code: 'atlas_snapshot_unavailable' } },
      'GET /v1/atlas/concepts': { body: F.CONCEPTS },
      'GET /v1/links/FRA': { status: 404, body: F.NOT_FOUND_ROUTE },
      'GET /v1/locations': { body: F.LOCATIONS },
    });
    at('/v3/pays/FRA');
    expect(await screen.findByText('Aucune donnée chargée')).toBeInTheDocument();
    expect(await screen.findAllByText('Pas encore disponible')).not.toHaveLength(0);
  });
});

describe('Demander', () => {
  it('answers with citation chips, the sourced-only banner and a collapsible plan', async () => {
    mockFetch({ 'POST /v1/ask': { body: F.ASK_BRESIL } });
    at('/v3/demander');
    expect(await screen.findByTestId('sourced-only')).toHaveTextContent('Réponse construite uniquement à partir de sources citées.');
    await userEvent.type(screen.getByLabelText('Votre question'), 'Quelle est la langue officielle du Brésil ?');
    await userEvent.click(screen.getByRole('button', { name: 'Demander' }));
    const answer = await screen.findByTestId('ask-answer');
    const sentences = within(answer).getAllByTestId('answer-sentence');
    expect(sentences).toHaveLength(3);
    expect(within(sentences[0]!).getByRole('link', { name: 'E1' })).toHaveAttribute('href', '#ev-E1');
    expect(sentences[1]).toHaveAttribute('data-kind', 'missing');
    expect(within(answer).getAllByTestId('evidence-item')[0]).toHaveTextContent('Wikidata Q155$FIXTURE-1');
    expect(within(answer).getByText(/Plan de requête/)).toBeInTheDocument();
  });
});

describe('Home', () => {
  it('lists every V3 screen and the availability of services', async () => {
    mockFetch({ 'GET /v1/kg/search': { body: F.SEARCH_BRESIL }, 'GET /v1/atlas/concepts': { body: F.CONCEPTS }, 'GET /v1/atlas/coverage': { status: 404, body: F.NOT_FOUND_ROUTE }, 'GET /v1/links/FRA': { body: F.LINKS_FRA } });
    at('/v3');
    for (const name of ['Neurones', 'Chemin', 'Frise', 'Atlas', 'Dossier pays', 'Demander']) expect((await screen.findAllByRole('link', { name: new RegExp(name) })).length).toBeGreaterThan(0);
    expect(await screen.findByText('pas encore disponible')).toBeInTheDocument();
  });
});
