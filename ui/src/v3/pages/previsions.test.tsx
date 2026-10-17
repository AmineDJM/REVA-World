import { configure, render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter, Route, Routes } from 'react-router';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { mockFetch } from '../../test/fetchMock';
import V3App from '../V3App';
import { fr } from './PrevisionsPage';

configure({ asyncUtilTimeout: 5000 });
vi.setConfig({ testTimeout: 20_000 });
afterEach(() => vi.unstubAllGlobals());

const env = <T,>(data: T) => ({
  data,
  context_id: null,
  context_mode: 'evaluation',
  origin: 'live',
  modality: 'forecast',
  knowledge_cutoff: '2026-10-17T14:02:32Z',
  warnings: [
    { code: 'baseline_forecasts_only', message: 'BASELINE forecasts only' },
    { code: 'not_advice', message: 'not advice' },
  ],
  next_cursor: null,
});
const rel = { n_common: 0, ratio: null, skill: null };
const QID = 'fq_c6df5458b602958cc60ff3b4';
const Q = {
  question_id: QID,
  question_key: '2026-Q4|weo.cpi_inflation|FRA|2026|quantity',
  cycle: '2026-Q4',
  family: 'weo.cpi_inflation',
  concept_id: 'imf.inflation_cpi_avg',
  label_fr: 'Inflation (IPC, moyenne annuelle)',
  label_en: 'Inflation',
  geo: 'FRA',
  geo_label_fr: 'France',
  geo_label_en: 'France',
  reference_period: '2026',
  type: 'quantity',
  unit: '%',
  threshold: null,
  text_fr: 'Quelle sera la valeur publiée de « Inflation (IPC, moyenne annuelle) » pour France (FRA), période 2026 ? (%)',
  text_en: '…',
  expected_resolution: '2027-05-01',
  void_after: '2029-01-30',
  resolution_source: 'atlas.imf.weo',
  status: 'pending',
  outcome: null,
  n_forecasts: 4,
  batch_sha256: 'a'.repeat(64),
  issued_at: '2026-10-17T14:02:32Z',
};
const MODEL = (model: string, kind = 'baseline') => ({
  model,
  version: '1.0.0',
  description: 'd',
  kind,
  n_issued: 434,
  n_abstained: 0,
  n_scored: 0,
  mae: null,
  crps: null,
  log_score: null,
  brier: null,
  binary_log_score: null,
  interval_score: null,
  coverage: null,
  n_intervals: 0,
  by_type: {},
  mae_vs_persistence: rel,
  crps_vs_persistence: rel,
  brier_vs_persistence: rel,
});

const ROUTES = {
  'GET /v1/forecasts/families': {
    body: env([
      { key: 'weo.cpi_inflation', label_fr: 'Inflation (IPC, moyenne annuelle)', label_en: 'x', definition: 'Annual percent change', dataset_key: 'atlas.imf.weo', frequency: 'A', unit: '%', question_types: ['quantity'], horizons: [0, 1], countries: ['FRA'], status: 'available', status_reason: null },
      { key: 'fao.food_price_index', label_fr: 'Indice FAO des prix des produits alimentaires', label_en: 'x', definition: null, dataset_key: null, frequency: 'M', unit: 'index', question_types: ['quantity'], horizons: [1], countries: [], status: 'awaiting_source', status_reason: 'no connector yet' },
    ]),
  },
  'GET /v1/forecasts/batches': {
    body: env([{ batch_sha256: 'a'.repeat(64), context_id: 'c', cycle: '2026-Q4', origin: 'live', issued_at: '2026-10-17T14:02:32Z', recorded_at: '2026-10-17T14:02:33Z', n_questions: 434, by_type: { quantity: 223, interval: 169, binary: 54 }, label: 'x', models: [] }]),
  },
  'GET /v1/forecasts/leaderboard': {
    body: env({ label: 'x', baseline_for_skill: 'forecast.registry_persistence', questions: { pending: 434 }, models: [MODEL('forecast.registry_persistence'), MODEL('forecast.reference_imf_weo', 'external_reference')] }),
  },
  'GET /v1/forecasts/calibration': { body: env({ model: 'forecast.registry_persistence', family: null, n_scored: 0, pit_histogram: { bins: 10, counts: Array(10).fill(0), n: 0 }, reliability: { bins: [], n: 0 }, interval_coverage: { nominal: 0.8, empirical: null, n: 0 } }) },
  'GET /v1/forecasts/questions/': {
    body: env({
      question: { ...Q, definition: 'Annual percent change of the period-average consumer price index (IMF WEO PCPIPCH).', resolution: { note: 'Première édition du WEO où l’année est un réalisé.' } },
      forecasts: [
        { model: 'forecast.registry_persistence', status: 'issued', point: '1.2', interval: null, predictive: { family: 'normal', mu: '1.2', sigma: '0.8' }, score: null },
        { model: 'forecast.registry_seasonal_naive', status: 'abstained', reason: 'seasonal naive needs a monthly or quarterly series' },
      ],
      outcome: null,
      status: 'pending',
      seal: { batch_sha256: 'a'.repeat(64), issued_at: '2026-10-17T14:02:32Z', verified: true, label: 'x', data_tables: {} },
    }),
  },
  'GET /v1/forecasts/questions': { body: env({ total: 1, offset: 0, questions: [Q] }) },
  'GET /v1/forecasts/backtests': { body: env([]) },
  'GET /v1/forecasts/ensemble': {
    body: env({
      label: 'REVA ENSEMBLE', model: 'reva.ensemble', current_version: '2.0.0', rule: 'r', alpha: 0.05, holm_family_size: 0, n_ensemble_forecasts: 434, n_ensemble_scored: 0,
      weights_by_version: { '2.0.0': { 'weo.cpi_inflation': { equal_weights_insufficient_history: 434 } } },
      headline_families: {},
      cells: [
        { ensemble_version: '2.0.0', family: 'weo.cpi_inflation', type: 'quantity', metric: 'crps', n_ensemble_issued: 434, n_ensemble_scored: 0, min_pairs: 20, competitors: [],
          best_single: null, test: null, status: 'awaiting_resolutions', p_holm: null, holm_family_size: 0, verdict: 'awaiting_resolutions', claim_fr: null },
      ],
    }),
  },
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

describe('Prévisions', () => {
  it('lists the sealed batch, the leaderboard (n. a., never 0), questions and opens a frozen forecast with its abstentions', async () => {
    mockFetch(ROUTES);
    at('/v3/previsions');
    expect(await screen.findByRole('heading', { name: 'Prévisions', level: 1 })).toBeInTheDocument();
    expect(screen.getByTestId('previsions-honesty')).toHaveTextContent('Prévisions de référence (baselines) uniquement');
    expect(screen.getByTestId('previsions-honesty')).toHaveTextContent('pas un conseil financier');
    const batches = await screen.findByTestId('previsions-batches');
    expect(batches).toHaveTextContent('2026-Q4');
    expect(batches).toHaveTextContent('434');
    const lb = await screen.findByTestId('previsions-leaderboard');
    expect(within(lb).getByText('Persistance (marche aléatoire)')).toBeInTheDocument();
    expect(lb).toHaveTextContent('source externe');
    expect(within(lb).getAllByText('n. a.').length).toBeGreaterThan(0);
    expect(await screen.findByTestId('previsions-counts')).toHaveTextContent('Aucune question n’est encore résolue');
    const ens = await screen.findByTestId('previsions-ensemble');
    expect(ens).toHaveTextContent('poids égaux (historique insuffisant)');
    expect(ens).toHaveTextContent('en attente de résolutions');
    expect(ens).toHaveTextContent('un poids par source d’information');
    const fam = await screen.findByTestId('previsions-families');
    expect(fam).toHaveTextContent('source à créer');
    const qs = await screen.findByTestId('previsions-questions');
    await userEvent.click(within(qs).getByRole('button', { name: /France \(FRA\)/ }));
    const detail = await screen.findByTestId('previsions-detail');
    expect(detail).toHaveTextContent('vérifié à la lecture');
    expect(detail).toHaveTextContent('Abstention : seasonal naive needs a monthly or quarterly series');
    expect(detail).toHaveTextContent('« Annual percent change');
  });

  it('formats not-applicable as n. a.', () => {
    expect(fr(null)).toBe('n. a.');
    expect(fr(0)).toBe('0');
  });
});
