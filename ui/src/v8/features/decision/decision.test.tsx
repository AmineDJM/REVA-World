/**
 * Decision studio (V8DECISION). Fixtures are REAL responses of /v1/v8/decision/* captured on the dev DB
 * (2026-10-18; hop provenance slimmed exactly as the server now sends it; ECB comparison trimmed to 8 rows).
 */
import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { MemoryRouter, Route, Routes, useLocation } from 'react-router';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import compareEcb from './__fixtures__/compare_ecb.json';
import runEu from './__fixtures__/run_eu.json';
import runIndia from './__fixtures__/run_india.json';
import runOpec from './__fixtures__/run_opec.json';
import sensOpec from './__fixtures__/sens_opec.json';
import templates from './__fixtures__/templates.json';
import { Templates as TemplatesSchema, RunBundle } from './api';
import DecisionStudio, { resetTemplates } from './DecisionStudio';
import { feature, variantHref } from './index';
import { filterGeo, layout, matrix, num, tornado } from './model';
import { adaptPluginOptions, parseState, resolveScenario, stateRef, stateToParams, validateFields } from './state';

const calls: { method: string; path: string; body: unknown }[] = [];
vi.mock('../../../api/http', () => ({
  request: vi.fn(async (method: string, path: string, opts: { body?: unknown } = {}) => {
    calls.push({ method, path, body: opts.body });
    const b = opts.body as Record<string, unknown> | undefined;
    if (path.endsWith('/templates')) return { status: 200, body: templates, headers: new Headers() };
    if (path.endsWith('/run')) {
      const ref = b?.reference_id ?? (b?.decision as { id?: string } | undefined)?.id ?? '';
      const fx = String(ref).startsWith('india') ? runIndia : String(ref).startsWith('opec') ? runOpec : runEu;
      return { status: 200, body: fx, headers: new Headers() };
    }
    if (path.endsWith('/sensitivity')) return { status: 200, body: sensOpec, headers: new Headers() };
    if (path.endsWith('/compare')) return { status: 200, body: compareEcb, headers: new Headers() };
    if (path.endsWith('/brief')) return { status: 200, body: { data: { markdown: '# Brief\n\n« citation »\n', filename: 'b.md', scenario_hash: 'x', label: 'scénario non validé — pas une probabilité', n_citations: 2 } }, headers: new Headers() };
    throw new Error(`unexpected ${path}`);
  }),
}));
vi.mock('../tableaux/pin', () => ({ pinItem: vi.fn(async () => ({ boardId: 'b1', boardTitle: 'Mes épingles', added: true, origin: 'local', href: '/v8/tableaux/b1' })) }));
vi.mock('cytoscape', () => {
  const cy = { on: vi.fn(), destroy: vi.fn(), zoom: () => 1, pan: vi.fn(), $id: () => ({ addClass: vi.fn(), position: () => ({ x: 0, y: 0 }) }), $: () => ({ removeClass: vi.fn() }) };
  return { default: vi.fn(() => cy) };
});

const T = TemplatesSchema.parse(templates.data);

function Loc() {
  const l = useLocation();
  return <output data-testid="loc">{l.search}</output>;
}
function mount(url: string) {
  return render(
    <MemoryRouter initialEntries={[url]}>
      <Routes>
        <Route
          path="/v8/decision"
          element={
            <>
              <DecisionStudio />
              <Loc />
            </>
          }
        />
      </Routes>
    </MemoryRouter>,
  );
}
const search = () => new URLSearchParams(screen.getByTestId('loc').textContent ?? '');

beforeEach(() => {
  calls.length = 0;
  resetTemplates();
});
afterEach(() => vi.clearAllMocks());

describe('fixtures are valid against the client contract', () => {
  it('parses every captured bundle', () => {
    for (const fx of [runEu, runIndia, runOpec]) {
      const b = RunBundle.parse(fx.data);
      expect(b.result.label).toBe('scénario non validé — pas une probabilité');
      expect(b.honesty.validated).toBe(false);
    }
  });
});

describe('URL state', () => {
  it('round-trips a reference, a variant and a comparison', () => {
    const p = new URLSearchParams('ref=ecb_plus_50bp_2027q1&m=25&b.ref=ecb_plus_50bp_2027q1&tab=comparer&v=x');
    const st = parseState(p);
    expect(st.a).toEqual({ ref: 'ecb_plus_50bp_2027q1', m: '25' });
    expect(st.b).toEqual({ ref: 'ecb_plus_50bp_2027q1' });
    expect(stateToParams(st, p).toString()).toBe('ref=ecb_plus_50bp_2027q1&m=25&b.ref=ecb_plus_50bp_2027q1&tab=comparer&v=x');
    // a nested state ref (boards pin composed scenarios this way) is unfolded
    const nested = parseState(new URLSearchParams({ ref: stateRef({ i: 'policy_rate_change', m: '25', u: 'bp' }) }));
    expect(nested.a).toEqual({ i: 'policy_rate_change', m: '25', u: 'bp' });
    expect(stateRef({ ref: 'ecb_plus_50bp_2027q1' })).toBe('ecb_plus_50bp_2027q1');
  });

  it('resolves an unchanged reference to reference_id and a variant to a full decision', () => {
    const r = resolveScenario({ ref: 'ecb_plus_50bp_2027q1' }, T);
    expect(r.kind).toBe('reference');
    expect(r.body).toEqual({ reference_id: 'ecb_plus_50bp_2027q1' });
    const v = resolveScenario({ ref: 'ecb_plus_50bp_2027q1', m: '25' }, T);
    expect(v.kind).toBe('variant');
    expect(v.changed).toEqual(['m']);
    const d = v.body!.decision as Record<string, unknown>;
    expect(d.id).toMatch(/^ecb_plus_50bp_2027q1\.variant-[0-9a-f]{8}$/);
    expect((d.magnitude as { value: string }).value).toBe('25');
    expect(d.countries).toEqual(['DEU', 'FRA', 'ITA', 'ESP', 'NLD']);
    expect(String(d.label_fr)).toContain('variante');
  });

  it('validates like the server schema, in French', () => {
    const ins = T.instruments.find((i) => i.id === 'export_ban');
    const p = validateFields({ i: 'export_ban', m: '0.5', u: 'share_of_exports_banned', s: '2027-01', h: '20', c: 'india' }, ins, T.schema);
    expect(p.join(' ')).toMatch(/mesure complète/);
    expect(p.join(' ')).toMatch(/2027Q1/);
    expect(p.join(' ')).toMatch(/horizon va de 1 à 12/i);
    expect(p.join(' ')).toMatch(/INDIA/);
    expect(p.join(' ')).toMatch(/pays de l’acteur/);
    const rate = T.instruments.find((i) => i.id === 'policy_rate_change');
    expect(validateFields({ i: 'policy_rate_change', m: '25', u: 'bp', s: '2027Q1', h: '8', c: '' }, rate, T.schema).join(' ')).toMatch(/au moins un pays/);
    expect(validateFields({ i: 'policy_rate_change', m: '25', u: 'bp', s: '2027Q1', h: '8', c: 'DEU,FRA' }, rate, T.schema)).toEqual([]);
  });

  it('drops a reference value derived from the changed magnitude (France: first cohort)', () => {
    expect(adaptPluginOptions({ 'social.retirement_age': { first_birth_year: 1962, reference_year: 2024 } }, ['m'])).toEqual({ 'social.retirement_age': { reference_year: 2024 } });
    expect(adaptPluginOptions({ 'social.retirement_age': { first_birth_year: 1962 } }, ['c'])).toEqual({ 'social.retirement_age': { first_birth_year: 1962 } });
  });
});

describe('model helpers', () => {
  it('formats French numbers and never turns missing into 0', () => {
    expect(num(-0.25, true)).toBe('−0,25');
    expect(num(12.5, true)).toBe('+12,5');
    expect(num(null)).toBeNull();
  });
  it('lays out the propagation graph by path length and filters by country', () => {
    const b = RunBundle.parse(runOpec.data);
    const placed = layout(b.graph.nodes, b.graph.edges);
    const col = (id: string) => placed.find((p) => p.node.id === id)!.col;
    expect(col('decision')).toBe(0);
    expect(col('variable:oil_price.global@WLD')).toBeGreaterThan(col('atlas_concept:resources.energy.eia.crude_oil_production@WLD'));
    expect(filterGeo(b.graph.nodes, b.graph.edges, 'FRA').nodes.every((n) => ['', 'WLD', 'FRA'].includes(n.geo))).toBe(true);
    const m = matrix(b.headline);
    expect(m.geos).toEqual(['WLD']);
  });
  it('builds tornado bars only from published uncertainty', () => {
    const s = sensOpec.data;
    const v = s.variables.find((x) => x.id === 'variable:oil.world_consumption@WLD')!;
    const t = tornado(v as never, v.headline_period!);
    expect(t.bars).toHaveLength(1);
    expect(t.bars[0]!.lo).toBeLessThan(t.bars[0]!.hi);
    expect(t.flat.map((f) => f.key)).toEqual(['pub:effects.literature:caldara_cavallo_iacoviello_2016:oil_price_response_to_supply_decline_08mbd']);
  });
});

describe('palette', () => {
  it('declares the route contract and variant commands', () => {
    expect(feature.path).toBe('decision');
    expect(feature.order).toBe(50);
    expect(feature.goKey).toBe('s');
    expect(variantHref('bce 25')?.href).toBe('/v8/decision?ref=ecb_plus_50bp_2027q1&m=25');
    expect(variantHref('opep 1')?.href).toBe('/v8/decision?ref=opec_plus_cut_2mbd_2027q1&m=-1');
    expect(variantHref('riz')).toBeNull();
  });
  it('searches reference decisions locally', async () => {
    const hits = await feature.search!('riz', new AbortController().signal);
    expect(hits[0]!.href).toBe('/v8/decision?ref=india_rice_export_ban_2027q1');
    expect(hits[0]!.group).toBe('Décisions');
  });
});

describe('studio screen', () => {
  it('shows the five templates and an empty state without a scenario (no request to run)', async () => {
    mount('/v8/decision');
    expect(await screen.findByRole('list', { name: 'Décisions de référence' })).toBeInTheDocument();
    expect(screen.getAllByRole('listitem')).toHaveLength(5);
    expect(screen.getByText('Choisissez une décision')).toBeInTheDocument();
    expect(calls.some((c) => c.path.endsWith('/run'))).toBe(false);
  });

  it('runs a reference from the URL: label, effects with competing estimates side by side, evidence quote', async () => {
    mount('/v8/decision?ref=eu_ban_russian_oil_2027q1');
    expect(await screen.findByText(/2 variable\(s\) chiffrée\(s\)/)).toBeInTheDocument();
    expect(calls.find((c) => c.path.endsWith('/run'))!.body).toEqual({ reference_id: 'eu_ban_russian_oil_2027q1' });
    expect(screen.getAllByText('scénario non validé — pas une probabilité').length).toBeGreaterThan(0);
    expect(screen.getByRole('table', { name: /Effets chiffrés/ })).toBeInTheDocument();
    // propagation tab: the edge list, the evidence panel with the verbatim quote and both estimates side by side
    fireEvent.click(screen.getByRole('tab', { name: /Propagation/ }));
    await waitFor(() => expect(search().get('tab')).toBe('propagation'));
    const edges = screen.getByRole('table', { name: /Liens de propagation/ });
    fireEvent.click(within(edges).getAllByRole('button')[1]!);
    const side = screen.getByRole('complementary', { name: /Preuve/ });
    expect(within(side).getByText(/Estimations concurrentes du même lien/)).toBeInTheDocument();
    expect(within(side).getByText('retenue')).toBeInTheDocument();
    expect(within(side).getByText('non retenue')).toBeInTheDocument();
    expect(search().get('e')).toBeTruthy();
  });

  it('explains why there is no result (rice ban): the missing link and the near estimate', async () => {
    mount('/v8/decision?ref=india_rice_export_ban_2027q1');
    expect(await screen.findAllByText(/Aucun résultat chiffré/)).not.toHaveLength(0);
    expect(screen.getByText(/Il manque :/)).toBeInTheDocument();
    expect(screen.getAllByText(/trade\.export_restriction/).length).toBeGreaterThan(0);
    expect(screen.getByText(/estimation\(s\) voisine\(s\)/)).toBeInTheDocument();
  });

  it('opens sensitivity, keyboard switches tabs, hatched rows for unpublished uncertainty', async () => {
    mount('/v8/decision?ref=opec_plus_cut_2mbd_2027q1');
    await screen.findByText(/variable\(s\) chiffrée\(s\)/);
    act(() => {
      window.dispatchEvent(new KeyboardEvent('keydown', { key: '3', code: 'Digit3' }));
    });
    await waitFor(() => expect(search().get('tab')).toBe('sensibilite'));
    expect(await screen.findAllByText(/incertitude non publiée — pas de barre/)).not.toHaveLength(0);
  });

  it('compares A and B from a preset and keeps both in the URL', async () => {
    mount('/v8/decision?ref=ecb_plus_50bp_2027q1&tab=comparer');
    fireEvent.click(await screen.findByRole('button', { name: 'BCE +25 pb contre +50 pb' }));
    await waitFor(() => expect(search().get('b.ref')).toBe('ecb_plus_50bp_2027q1'));
    expect(search().get('m')).toBe('25');
    expect(await screen.findByRole('table', { name: /Comparaison A \/ B/ })).toBeInTheDocument();
    const cmp = calls.find((c) => c.path.endsWith('/compare'))!.body as { a: { decision: { magnitude: { value: string } } }; b: { reference_id: string } };
    expect(cmp.a.decision.magnitude.value).toBe('25');
    expect(cmp.b.reference_id).toBe('ecb_plus_50bp_2027q1');
  });

  it('refuses an invalid composed decision before any request', async () => {
    mount('/v8/decision?i=export_ban&m=0.5&u=share_of_exports_banned&s=2027Q1&h=8&ag=IND');
    expect(await screen.findByText(/mesure complète/)).toBeInTheDocument();
    expect(calls.some((c) => c.path.endsWith('/run'))).toBe(false);
  });

  it('pins the scenario to a board by its ref', async () => {
    const { pinItem } = await import('../tableaux/pin');
    mount('/v8/decision?ref=eu_ban_russian_oil_2027q1');
    await screen.findByText(/2 variable\(s\) chiffrée\(s\)/);
    fireEvent.click(screen.getByRole('button', { name: 'Épingler' }));
    await screen.findByText(/Épinglé sur « Mes épingles »/);
    expect(pinItem).toHaveBeenCalledWith(expect.objectContaining({ kind: 'decision', ref: 'eu_ban_russian_oil_2027q1' }));
  });
});
