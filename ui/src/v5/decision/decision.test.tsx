import { configure, render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter, Route, Routes } from 'react-router';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { mockFetch } from '../../test/fetchMock';
import V3App from '../../v3/V3App';
import { Prediction } from './api';
import { bands, composeDecision, formFromDecision } from './model';

configure({ asyncUtilTimeout: 5000 });
vi.setConfig({ testTimeout: 20_000 });
afterEach(() => vi.unstubAllGlobals());

const LABEL = 'scénario non validé — pas une probabilité';

const REF = {
  id: 'ecb_plus_50bp_2027q1',
  label_fr: 'La BCE relève ses taux directeurs de 50 points de base au 1er trimestre 2027',
  actor: { label_fr: 'Banque centrale européenne', geo: null, kg_id: null },
  instrument: 'policy_rate_change',
  target: { label_fr: 'Taux directeur de la zone euro', geo: null, kg_id: null },
  magnitude: { value: '50', unit: 'bp' },
  start: '2027Q1',
  duration_quarters: null,
  horizon_quarters: 4,
  countries: ['FRA'],
};

const DECISIONS = {
  data: {
    decisions: [REF],
    instruments: [
      {
        id: 'policy_rate_change',
        label_fr: 'Variation du taux directeur',
        label_en: 'Policy-rate change',
        magnitude_units: ['bp', 'pp'],
        direct_changes: [{ geo: 'EACH', basis_fr: 'Hypothèse de scénario : le taux varie de la grandeur décidée.' }],
        plugins: [],
      },
    ],
    label: LABEL,
  },
  warnings: [],
};

const q = (p50: number) => ({ p05: p50 - 0.1, p25: p50 - 0.05, p50, p75: p50 + 0.05, p95: p50 + 0.1, mean: p50, degenerate: false });

/** SYNTHETIC engine output (shape of src/reva/predict/engine.py), no real-world estimate. */
const PREDICTION = {
  engine: 'reva.predict.engine@1.0.0',
  label: LABEL,
  validation: { status: 'unvalidated', label: LABEL, chain_signature: 'ab'.repeat(32), reason: 'no record', judge_requirements: ['as-of replay', 'calibration record'] },
  disclaimers_fr: ['Recherche, pas un conseil financier ni une recommandation.'],
  decision: { id: REF.id, label_fr: REF.label_fr },
  settings: { draws: 2000, seed: 0, periods: ['2027Q1', '2027Q2', '2027Q3', '2027Q4'] },
  direct_changes: [{ variable: { kind: 'atlas_concept', id: 'imf.policy_rate' }, geo: 'FRA', change: '0.5', unit: 'percentage_points', basis_fr: 'Hypothèse de scénario : le taux varie.' }],
  variables: [
    {
      id: 'atlas_concept:imf.inflation_cpi_avg@FRA',
      variable: { kind: 'atlas_concept', id: 'imf.inflation_cpi_avg', label_fr: 'Inflation (IPC, moyenne)', label_en: null },
      geo: 'FRA',
      unit: 'pp',
      kind: 'estimate_chain',
      frequency: 'quarter',
      depth: 1,
      path: ['h1', 'h2'],
      periods: [
        { period: '2027Q1', missing_reason: null, ...q(-0.1) },
        { period: '2027Q2', missing_reason: null, ...q(-0.2) },
        { period: '2027Q3', missing_reason: 'lag_not_published (lag 2 quarters)' },
        { period: '2027Q4', missing_reason: 'lag_not_published (lag 3 quarters)' },
      ],
      undated: null,
      baseline: null,
      uncertainty_complete: true,
      point_estimates: [],
      notes: [],
    },
    {
      id: 'atlas_concept:imf.unemployment_rate@FRA',
      variable: { kind: 'atlas_concept', id: 'imf.unemployment_rate', label_fr: 'Taux de chômage', label_en: null },
      geo: 'FRA',
      unit: 'pp',
      kind: 'estimate_chain',
      frequency: 'undated',
      depth: 2,
      path: ['h1', 'h2', 'h3'],
      periods: [],
      undated: { kind: 'long_run', kind_label_fr: 'effet de long terme (non daté)', ...q(0.3), degenerate: true },
      baseline: null,
      uncertainty_complete: false,
      point_estimates: ['pub:test:okun'],
      notes: [],
    },
  ],
  hops: [
    { id: 'h1', kind: 'decision', to: 'atlas_concept:imf.policy_rate@FRA', direct_change: { basis_fr: 'Hypothèse de scénario : le taux varie.' } },
    {
      id: 'h2',
      kind: 'estimate',
      from: 'atlas_concept:imf.policy_rate@FRA',
      to: 'atlas_concept:imf.inflation_cpi_avg@FRA',
      estimate_key: 'pub:test:rate_infl',
      source: { estimate_key: 'pub:test:rate_infl', source_kind: 'publication', source_key: 'test', wording_fr: 'Selon une source synthétique, une hausse de taux réduit l’inflation ; REVA n’établit pas cet effet.', url: 'https://example.org/paper', locator: 'Table 1' },
      coefficient: { point: -0.4, sd: 0.1, uncertainty_basis: 'se', in_unit: 'pp', out_unit: 'pp' },
      applicability: { status: 'exact', notes: [], geo: 'FRA' },
      alternatives: [{ estimate_key: 'pub:test:other', reason_fr: 'non retenue' }],
      notes: [],
    },
    {
      id: 'h3',
      kind: 'estimate',
      from: 'atlas_concept:imf.inflation_cpi_avg@FRA',
      to: 'atlas_concept:imf.unemployment_rate@FRA',
      estimate_key: 'pub:test:okun',
      source: { estimate_key: 'pub:test:okun', source_kind: 'publication', source_key: 'test', wording_fr: 'Selon une source synthétique, … ; REVA n’établit pas cet effet.' },
      coefficient: { point: -1.5, sd: null, uncertainty_basis: null },
      applicability: { status: 'group_unverified', notes: [], geo: 'FRA' },
      alternatives: [],
      notes: [],
    },
  ],
  unquantified_links: [
    {
      kind: 'causal_claim',
      claim_key: 'kg:test:1',
      wording_fr: 'Selon Wikidata, la crise A a eu pour effet B.',
      note_fr: 'affirmation causale sourcée sur un épisode analogue : aucun nombre propagé',
      grade: 'D',
      source: { source_key: 'wikidata.wbapi', tier: 'T4' },
    },
  ],
  excluded_links: [{ code: 'out_of_scope', estimate_key: 'pub:test:deu', detail: 'estimate scope [DEU] does not include FRA', geo: 'FRA' }],
  alternative_paths: [],
  gaps: [{ code: 'no_effect_estimate_for_cause', node: 'x', message_fr: 'aucune estimation d’effet n’a pour cause imf.unemployment_rate : la propagation s’arrête ici' }],
  effect_layer: { layer: 'present', detail: 'kg.effect_estimate_current' },
  chain: ['pub:test:okun', 'pub:test:rate_infl'],
};

function at(path: string) {
  return render(
    <MemoryRouter initialEntries={[path]}>
      <Routes>
        <Route path="/v3/*" element={<V3App />} />
      </Routes>
    </MemoryRouter>,
  );
}

describe('Décision screen', () => {
  it('propagates a reference decision and shows label, fan chart, causal path, unquantified links and gaps', async () => {
    const { calls } = mockFetch({
      'GET /v1/predict/decisions': { body: DECISIONS },
      'POST /v1/predict/decision': { body: { data: PREDICTION, warnings: [{ code: 'unvalidated_scenario', message: LABEL }] } },
    });
    at('/v3/decision');
    expect(await screen.findByTestId('engine-label')).toHaveTextContent(LABEL);
    await userEvent.click(screen.getByRole('button', { name: 'Propager la décision' }));
    const result = await screen.findByTestId('prediction');
    const post = calls.find((c) => c.method === 'POST')!;
    expect(post.body).toEqual({ reference_id: REF.id, draws: 2000, seed: 0 });
    expect(within(result).getByRole('heading', { name: `Statut : ${LABEL}` })).toBeInTheDocument();
    expect(within(result).getByTestId('fan-chart')).toHaveAttribute('aria-label', expect.stringContaining('Inflation'));
    expect(within(result).getByTestId('interval-bar')).toBeInTheDocument();
    expect(within(result).getByText(/Distribution incomplète/)).toBeInTheDocument();
    // missing periods are said, never drawn as zero
    expect(within(result).getAllByText(/délai non publié par la source/).length).toBe(2);
    const hops = within(result).getAllByTestId('estimate-hop');
    expect(hops[0]).toHaveTextContent('pub:test:rate_infl');
    expect(hops[0]).toHaveTextContent('non additionnée');
    expect(within(hops[0]!).getByRole('link', { name: 'source' })).toHaveAttribute('href', 'https://example.org/paper');
    expect(within(result).getByTestId('unquantified')).toHaveTextContent('Selon Wikidata, la crise A a eu pour effet B.');
    expect(within(result).getByTestId('gaps')).toHaveTextContent('la propagation s’arrête ici');
  });

  it('composes a decision, refusing invalid input before any request', async () => {
    const { calls } = mockFetch({ 'GET /v1/predict/decisions': { body: DECISIONS } });
    at('/v3/decision');
    const start = await screen.findByLabelText('Début (trimestre)');
    await userEvent.clear(start);
    await userEvent.type(start, '2027-01');
    await userEvent.click(screen.getByRole('button', { name: 'Propager la décision' }));
    expect(await screen.findByRole('alert')).toHaveTextContent('trimestre de la forme 2027Q1');
    expect(calls.filter((c) => c.method === 'POST')).toHaveLength(0);
  });

  it('says plainly when nothing could be quantified', async () => {
    mockFetch({
      'GET /v1/predict/decisions': { body: DECISIONS },
      'POST /v1/predict/decision': { body: { data: { ...PREDICTION, variables: [], unquantified_links: [] }, warnings: [] } },
    });
    at('/v3/decision');
    await userEvent.click(await screen.findByRole('button', { name: 'Propager la décision' }));
    expect(await screen.findByTestId('no-variable')).toHaveTextContent('REVA n’invente pas de nombre');
  });
});

describe('decision model and contract', () => {
  it('builds the request decision and never sends a malformed one', () => {
    const f = formFromDecision(REF);
    const ok = composeDecision({ ...f, id: '', countries: 'fra, deu' });
    expect(ok.problems).toEqual([]);
    expect(ok.decision).toMatchObject({ instrument: 'policy_rate_change', countries: ['FRA', 'DEU'], duration_quarters: null, horizon_quarters: 4 });
    expect(composeDecision({ ...f, magnitude: 'cinquante', countries: 'FRANCE' }).problems).toHaveLength(2);
  });

  it('keeps missing periods apart from drawn ones', () => {
    const { drawn, missing } = bands(PREDICTION.variables[0]!.periods as never);
    expect(drawn.map((d) => d.period)).toEqual(['2027Q1', '2027Q2']);
    expect(missing.map((d) => d.period)).toEqual(['2027Q3', '2027Q4']);
  });

  it('rejects an unquantified link that carries a number, and a result without its label', () => {
    const bad = { ...PREDICTION, unquantified_links: [{ ...PREDICTION.unquantified_links[0], point: 0.3 }] };
    expect(Prediction.safeParse(bad).success).toBe(false);
    expect(Prediction.safeParse({ ...PREDICTION, label: '' }).success).toBe(false);
    expect(Prediction.safeParse(PREDICTION).success).toBe(true);
  });
});
