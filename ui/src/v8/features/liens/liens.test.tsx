import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { MemoryRouter, Route, Routes, useLocation } from 'react-router';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { ApiError } from '../../../api/problem';
import type { EgoData, Hop, PathsData, PathsResponse } from './api';
import { feature } from './index';
import { answerSentence, buildGraph, licenceShort, parseState, stateQuery, strengthShort, warningsFr } from './model';

// ------------------------------------------------------------------ fixtures (shapes of /v1/v8/paths; synthetic ids)
const node = (id: string, label: string, extra: Record<string, unknown> = {}) => ({
  id,
  label,
  label_en: label,
  label_loaded: true,
  kind: 'gene',
  kind_label: null,
  virtual: false,
  scope_iso3: null,
  degree: 6,
  ...extra,
  fiche: { kind: (extra.kind as string | undefined) ?? 'gene', id },
});

const CLAIM: Hop = {
  from: 'syn:gene',
  to: 'syn:disease',
  kind: 'causal_claim',
  direction: 'forward',
  cost: 0.5,
  node_penalty: 0,
  alternatives: 1,
  family: null,
  property: null,
  relation: { fr: 'a causé', en: 'caused' },
  sentence: { fr: 'Gène X — a causé → Maladie Y', en: 'Gene X — caused → Disease Y' },
  source: { key: 'syn.ontology', label: 'Ontologie de test', statement_id: 's1', revision: null, citation: null, retrieved_at: null },
  licence: { licence: 'CC BY 4.0', licence_url: null, attribution: null, restricted: false, required_notice: null },
  strength: { level: 'causal_claim', label: 'affirmation', grade: 'B', tier: 'T1', verification: null, reasons: ['règle r1'], method: null, uncertainty: null },
  dated: null,
  detail: { wording_fr: 'Selon la source, X a causé Y ; REVA ne l’établit pas.' },
  origin: 'live',
};
const STMT: Hop = {
  ...CLAIM,
  from: 'syn:disease',
  to: 'syn:drug',
  kind: 'statement',
  direction: 'inverse',
  cost: 2,
  alternatives: 0,
  family: { id: 'associated_with', label: { fr: 'est associé à', en: 'is associated with' }, inverse: false },
  property: { pid: 'syn:indication', label_fr: 'indication', label_en: 'indication' },
  relation: { fr: 'est associé à', en: 'is associated with' },
  sentence: { fr: 'Maladie Y — est associé à → Médicament Z', en: '' },
  licence: null,
  strength: { level: 'documented', label: 'déclaré', grade: null, tier: null, verification: 'source_reported_not_verified', reasons: null, method: null, uncertainty: null },
  detail: null,
};

const DATA: PathsData = {
  from: node('syn:gene', 'Gène X'),
  to: node('syn:drug', 'Médicament Z', { kind: 'drug' }),
  found: true,
  paths: [
    { rank: 1, cost: 2.5, length: 2, kind: 'mixed', nodes: ['syn:gene', 'syn:disease', 'syn:drug'], hops: [CLAIM, STMT], weakest_grade: 'B', restricted_licences: [], unknown_licences: 1, variants: null },
    {
      rank: 2,
      cost: 3.5,
      length: 3,
      kind: 'documented',
      nodes: ['syn:gene', 'syn:protein', 'syn:target', 'syn:drug'],
      hops: [
        { ...STMT, from: 'syn:gene', to: 'syn:protein', sentence: { fr: 'Gène X — produit → Protéine P', en: '' } },
        { ...STMT, from: 'syn:protein', to: 'syn:target', sentence: { fr: 'Protéine P — fait partie de → Cible T', en: '' } },
        { ...STMT, from: 'syn:target', to: 'syn:drug', sentence: { fr: 'Cible T — interagit avec → Médicament Z', en: '' } },
      ],
      weakest_grade: null,
      restricted_licences: ['CC BY-SA 3.0'],
      unknown_licences: 0,
      variants: { count: 3, position: 1, examples: ['syn:protein2'] },
    },
  ],
  nodes: {
    'syn:gene': node('syn:gene', 'Gène X'),
    'syn:disease': node('syn:disease', 'Maladie Y', { kind: 'disease' }),
    'syn:drug': node('syn:drug', 'Médicament Z', { kind: 'drug' }),
    'syn:protein': node('syn:protein', 'Protéine P'),
    'syn:protein2': node('syn:protein2', 'Protéine P2'),
    'syn:target': node('syn:target', 'Cible T'),
  },
  search: { max_hops: 4, k: 6, families: null, include_causal: true, levels: 4, entries_scanned: 120, nodes_reached: 80, hubs_not_expanded: [], hubs_not_expanded_count: 0, truncated: false, truncation_reasons: [], search_ms: 12 },
  not_found_reason: null,
  note: { fr: 'Un chemin est un ensemble de liens documentés, pas une preuve causale.', en: '' },
  index: { statements: 1000, causal_claims: 10, effect_estimates: 3, built_at: '2026-10-18T00:00:00Z' },
} as unknown as PathsData;

const RESPONSE = { data: DATA, warnings: [{ code: 'path_not_causal', message: 'x' }, { code: 'licence_restricted', message: 'Restricted licence(s): CC BY-SA 3.0' }] } as unknown as PathsResponse;

const EGO: EgoData = {
  center: node('syn:gene', 'Gène X'),
  edges: [{ hop: CLAIM, group: 'causal_claim' }],
  nodes: DATA.nodes,
  groups: { causal_claim: 1, produces: 4 },
} as unknown as EgoData;

const api = vi.hoisted(() => ({
  fetchPaths: vi.fn(),
  fetchNeighbourhood: vi.fn(),
  fetchFamilies: vi.fn(),
  searchNodes: vi.fn(),
  searchUniversal: vi.fn(),
}));
vi.mock('./api', async (orig) => ({ ...(await orig<typeof import('./api')>()), ...api }));

// ------------------------------------------------------------------ pure model
describe('liens model', () => {
  it('round-trips the URL state and omits defaults', () => {
    const s = parseState(new URLSearchParams('from=Q1&to=Q2&fam=part_of,located_in&causal=1&hops=5&p=2&n=Q3'));
    expect(s).toEqual({ from: 'Q1', to: 'Q2', families: ['located_in', 'part_of'], causal: true, hops: 5, path: 1, node: 'Q3' });
    expect(stateQuery(s).toString()).toBe('from=Q1&to=Q2&fam=located_in%2Cpart_of&causal=1&hops=5&p=2&n=Q3');
    expect(stateQuery(parseState(new URLSearchParams('from=Q1&to=Q2&hops=99&fam=BAD!'))).toString()).toBe('from=Q1&to=Q2');
  });

  it('answers first, with the shortest path and the attributed claims', () => {
    expect(answerSentence(DATA)).toMatch(/Gène X et Médicament Z sont reliés par 2 chemins \(le plus court : 2 liens\)/);
    expect(answerSentence(DATA)).toMatch(/via Maladie Y/);
    expect(answerSentence({ ...DATA, found: false, paths: [] })).toBe('Aucun chemin trouvé entre Gène X et Médicament Z.');
  });

  it('states evidence strength and never shows an unknown licence as open', () => {
    expect(strengthShort(CLAIM)).toBe('note B · T1');
    expect(strengthShort(STMT)).toBe('déclaré par la source');
    expect(licenceShort(STMT)).toEqual({ text: 'licence inconnue', restricted: false, unknown: true });
    expect(licenceShort(CLAIM).text).toBe('CC BY 4.0');
  });

  it('translates server warnings and drops the ones the page states itself', () => {
    const w = warningsFr(RESPONSE.warnings, { ...DATA, paths: [{ ...DATA.paths[1]! }] });
    expect(w).toEqual(['Licences restrictives à respecter pour réutiliser ces liens : CC BY-SA 3.0.']);
  });

  it('lays out paths left to right, one row per path, shared nodes once', () => {
    const g = buildGraph(DATA, [{ center: 'syn:drug', data: EGO }]);
    expect(g.nodes.get('syn:gene')!.x).toBe(0);
    expect(g.nodes.get('syn:drug')!.x).toBeGreaterThan(g.nodes.get('syn:disease')!.x);
    expect(g.nodes.get('syn:disease')!.y).not.toBe(g.nodes.get('syn:protein')!.y);
    const claimEdge = [...g.edges.values()].find((e) => e.tone === 'claim')!;
    expect(claimEdge.paths).toEqual([0]);
    expect(g.nodes.get('syn:gene')!.role).toBe('from');
  });
});

// ------------------------------------------------------------------ page
function Where() {
  const l = useLocation();
  return <output data-testid="where">{l.search}</output>;
}

async function renderAt(url: string) {
  const { default: LiensPage } = await import('./LiensPage');
  render(
    <MemoryRouter initialEntries={[url]}>
      <Routes>
        <Route
          path="/v8/liens"
          element={
            <>
              <LiensPage />
              <Where />
            </>
          }
        />
      </Routes>
    </MemoryRouter>,
  );
}

describe('LiensPage', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    api.fetchFamilies.mockResolvedValue([{ id: 'part_of', label: 'fait partie de', group: 'structure' }]);
    api.fetchPaths.mockResolvedValue(RESPONSE);
    api.fetchNeighbourhood.mockResolvedValue({ data: EGO, warnings: [] });
    api.searchNodes.mockResolvedValue([]);
    api.searchUniversal.mockResolvedValue([]);
  });

  it('shows examples and the honesty note when nothing is chosen', async () => {
    await renderAt('/v8/liens');
    expect(await screen.findByText(/Choisissez deux entités/)).toBeTruthy();
    expect(screen.getByTestId('lien-honesty').textContent).toMatch(/pas une preuve causale/);
    expect(screen.getAllByRole('button', { name: /→/ }).length).toBeGreaterThanOrEqual(5);
    expect(api.fetchPaths).not.toHaveBeenCalled();
  });

  it('renders the answer, readable chains with sources, licences and grades', async () => {
    await renderAt('/v8/liens?from=syn:gene&to=syn:drug&causal=1');
    expect((await screen.findByTestId('lien-answer')).textContent).toMatch(/reliés par 2 chemins/);
    expect(api.fetchPaths).toHaveBeenCalledWith(expect.objectContaining({ from: 'syn:gene', to: 'syn:drug', causal: true, hops: 4, families: [] }), expect.anything());
    expect(screen.getAllByTestId('lien-path')).toHaveLength(2);
    expect(screen.getByRole('button', { name: 'Épingler' })).toBeInTheDocument();
    expect(screen.getAllByText(/affirmation causale · note B · T1/).length).toBe(1);
    expect(screen.getByText(/maillon le plus faible : B/)).toBeTruthy();
    expect(screen.getAllByText('licence inconnue').length).toBeGreaterThan(0);
    expect(screen.getByText(/\+1 autre lien entre ces deux nœuds/)).toBeTruthy();
    expect(screen.getByText(/3 variantes/)).toBeTruthy();
    expect(screen.getByText(/Licences restrictives à respecter/)).toBeTruthy();
    expect(screen.getByTestId('lien-foot').textContent).toMatch(/120 liens examinés/);
  });

  it('keeps state in the URL: causal toggle, family chip, path selection by keyboard, node card', async () => {
    await renderAt('/v8/liens?from=syn:gene&to=syn:drug');
    await screen.findByTestId('lien-answer');
    fireEvent.click(screen.getByTestId('lien-causal'));
    await waitFor(() => expect(screen.getByTestId('where').textContent).toContain('causal=1'));
    fireEvent.click(await screen.findByRole('button', { name: 'fait partie de' }));
    await waitFor(() => expect(screen.getByTestId('where').textContent).toContain('fam=part_of'));
    await waitFor(() => expect(screen.getAllByTestId('lien-path')).toHaveLength(2));
    act(() => {
      fireEvent.keyDown(window, { key: 'j' });
    });
    await waitFor(() => expect(screen.getByTestId('where').textContent).toContain('p=2'));
    fireEvent.click(screen.getAllByRole('button', { name: 'Maladie Y' })[0]!);
    expect((await screen.findByTestId('lien-nodecard')).textContent).toMatch(/Maladie Y/);
    expect(screen.getByRole('link', { name: /Fiche/ }).getAttribute('href')).toBe('/v8/fiche/disease/syn%3Adisease');
    fireEvent.click(screen.getByRole('button', { name: /Développer/ }));
    await waitFor(() => expect(api.fetchNeighbourhood).toHaveBeenCalledWith('syn:disease', expect.anything()));
  });

  it('explores the neighbourhood when only one end is chosen', async () => {
    await renderAt('/v8/liens?from=syn:gene');
    expect(await screen.findByTestId('lien-ego')).toBeTruthy();
    expect(api.fetchPaths).not.toHaveBeenCalled();
    expect(screen.getByText(/Voisins de/)).toBeTruthy();
  });

  it('says when the server index is being built instead of showing an error', async () => {
    api.fetchPaths.mockRejectedValue(new ApiError('server', 503, { title: 'x', status: 503, code: 'paths_index_building', detail: 'building' }));
    await renderAt('/v8/liens?from=syn:gene&to=syn:drug');
    expect(await screen.findByText(/Index des liens en construction/)).toBeTruthy();
  });

  it('explains an empty result and offers to widen the search', async () => {
    api.fetchPaths.mockResolvedValue({ data: { ...DATA, found: false, paths: [], not_found_reason: 'aucun chemin de 4 liens au plus' }, warnings: [] });
    await renderAt('/v8/liens?from=syn:gene&to=syn:drug');
    expect(await screen.findByText('aucun chemin de 4 liens au plus')).toBeTruthy();
    expect(screen.getByRole('button', { name: /inclure les affirmations causales/ })).toBeTruthy();
    expect(screen.getByRole('button', { name: /autoriser 5 liens/ })).toBeTruthy();
  });
});

describe('feature', () => {
  it('declares route, order, go key and a palette command for two ids', () => {
    expect(feature).toMatchObject({ id: 'liens', path: 'liens', order: 40, goKey: 'l' });
    const cmd = feature.commands![0]!;
    expect(cmd.run('liens Q851 FRA')).toBe('/v8/liens?from=Q851&to=FRA');
    expect(cmd.run('liens')).toBe('/v8/liens');
  });
});
