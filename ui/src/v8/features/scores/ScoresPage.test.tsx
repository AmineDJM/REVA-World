import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { MemoryRouter, Route, Routes, useLocation } from 'react-router';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { CellDetail, LiveState, Row, ScoresPage as Page, TrackDetail } from './api';

const api = vi.hoisted(() => ({ getScores: vi.fn(), getTrack: vi.fn(), getCell: vi.fn(), getLive: vi.fn(), getDoc: vi.fn() }));
vi.mock('./api', async (orig) => ({ ...(await orig<typeof import('./api')>()), ...api }));

import ScoresPage from './ScoresPage';

const counts = (bat: number, ne: number, eg: number, att: number) => ({ bat, ne_bat_pas: ne, egalite: eg, en_attente: att });
const vocab = {
  verdicts: { bat: 'bat', ne_bat_pas: 'ne bat pas', egalite: 'égalité', en_attente: 'en attente' },
  evaluations: { preregistered_holdout: 'holdout pré-enregistré', development: 'développement', prospective: 'prospectif' },
  benchmark_kinds: { official_point: 'point officiel', weak_reference: 'repère faible', naive: 'naïf' },
  candidate_kinds: { combination_with_official: 'combinaison', pure_reva: 'REVA seul' },
  metrics: { ae: 'erreur absolue', crps: 'CRPS' },
  caveats: { uses_official_forecast: 'Utilise la prévision officielle en entrée.', timeliness_edge: 'Avantage de fraîcheur.' },
};

const WIN: Row = {
  id: 'beat_macro.holdout.x', track: 'beat_macro', target: 'macro.g7_unemployment', forecaster: 'imf_weo', benchmark: 'benchmark.imf_weo',
  benchmark_label_fr: 'point WEO en vigueur à t', benchmark_kind: 'official_point', candidate: 'forecast.bm_stack', candidate_label_fr: 'Combinaison officiel + nowcast REVA',
  candidate_kind: 'combination_with_official', evaluation: 'preregistered_holdout', metric: 'ae', ratio: 0.629713, p_raw: 0.0044, p_holm: 0.0265, holm_family_size: 7,
  holm_rejected: true, verdict: 'bat', verdict_basis_fr: 'confirmé : Holm ✓ (famille 7), ratio < 1', caveats: ['uses_official_forecast', 'timeliness_edge'],
  source: { file: 'registry/forecasts/beat_macro/holdout.json', pointer: '/results/1', sha256: 'a'.repeat(64) }, horizon_label_fr: 'année en cours',
} as Row;
const LOSS: Row = { ...WIN, id: 'beat_macro.holdout.y', target: 'macro.g7_cpi', verdict: 'ne_bat_pas', ratio: 0.97, holm_rejected: false, verdict_basis_fr: 'non confirmé' } as Row;

const PAGE: Page = {
  headline: counts(1, 1, 0, 1), headline_rule_fr: 'Comparaisons jugées contre un prévisionniste officiel.',
  by_evaluation: { preregistered_holdout: counts(1, 1, 0, 0), prospective: counts(0, 0, 0, 1) }, weak_reference: counts(3, 9, 0, 0), naive: counts(12, 18, 0, 3), n_rows: 5,
  wins: [{ id: WIN.id, track: 'beat_macro', target: WIN.target, forecaster: 'imf_weo', candidate_label_fr: WIN.candidate_label_fr, benchmark_label_fr: WIN.benchmark_label_fr, candidate_kind: WIN.candidate_kind, evaluation: WIN.evaluation, metric: 'ae', ratio: 0.629713, p_holm: 0.0265, horizon_label_fr: 'année en cours', caveats: WIN.caveats }],
  forecasters: [{ id: 'imf_weo', label_fr: 'FMI — WEO', short: 'FMI' }, { id: 'usda_wasde', label_fr: 'USDA — WASDE', short: 'WASDE', status: 'planned' }],
  matrix: [
    { domain: 'macro', label_fr: 'Macroéconomie', targets: [
      { target: 'macro.g7_unemployment', label_fr: 'Chômage — G7', cells: { imf_weo: { key: 'macro.g7_unemployment~imf_weo', status: 'bat', counts: counts(1, 0, 0, 1), ratio_min: 0.63, ratio_max: 0.63, tracks: ['beat_macro'] } } },
      { target: 'macro.g7_cpi', label_fr: 'Inflation — G7', cells: { imf_weo: { key: 'macro.g7_cpi~imf_weo', status: 'ne_bat_pas', counts: counts(0, 1, 0, 0), ratio_min: 0.97, ratio_max: 0.97 } } },
    ] },
    { domain: 'agri', label_fr: 'Agriculture', targets: [
      { target: 'agri.wasde', label_fr: 'Bilans agricoles', cells: { usda_wasde: { key: 'agri.wasde~usda_wasde', status: 'no_track', reason_fr: 'Piste BEAT-AGRI annoncée, pas commencée.', quote: { file: 'docs/roadmap/v8_console.md', text: 'BEAT-AGRI vs USDA WASDE' } } } },
    ] },
  ],
  tracks: [{ id: 'beat_macro', label_fr: 'BEAT-MACRO', agent: 'BEAT-MACRO', version: 'V7', summary_fr: 'Même date.', counts: counts(1, 1, 0, 1), n_rows: 3 }],
  calendar: [
    { id: 'event.first', kind: 'event', date: '2026-10-22', note_fr: 'Premières résolutions prospectives (NHSN)', rows: [], n_rows: 0, forecasters: [], quote: { file: 'docs/dev/review_v6_predictions.md', text: 'x' } },
    { id: 'beat_health:2027-07-03', kind: 'track', track: 'beat_health', date: '2027-07-03', note_fr: 'lecture unique du holdout H1', rows: ['a', 'b'], n_rows: 2, forecasters: ['cdc_flusight'], sealed: null },
  ],
  vocab, sources: [], stale_sources: [], problems: [], quarantined: [], sealed_models: {},
} as unknown as Page;

const LIVE_FULL: LiveState = {
  computed_at: '2026-10-17T22:30:00+00:00', totals: { sealed_batches: 16, questions: 3610, forecasts: 14643, pending: 3605, resolved: 0, void: 5, not_prospective: 0 },
  next_resolution: '2026-10-18', upcoming: [{ date: '2026-10-18', family: 'nhsn.flu_hosp', label_fr: 'Hospitalisations grippe (NHSN)', n_questions: 53, officials: ['cdc_flusight'], registry: 'health', overdue: false }],
  upcoming_total: 1, overdue_groups: 0, refreshing: false, partial: false, registries: [], ensemble: { available: true, cells: 127, verdicts: { awaiting_resolutions: 127 }, n_ensemble_forecasts: 2393, n_ensemble_scored: 0 },
} as unknown as LiveState;

function Where() {
  const l = useLocation();
  return <output data-testid="where">{l.search}</output>;
}

function renderAt(url: string) {
  return render(
    <MemoryRouter initialEntries={[url]}>
      <Routes>
        <Route path="/v8/scores" element={<><ScoresPage /><Where /></>} />
      </Routes>
    </MemoryRouter>,
  );
}

beforeEach(() => {
  api.getScores.mockResolvedValue({ data: PAGE });
  api.getLive.mockResolvedValue({ data: LIVE_FULL });
  api.getCell.mockResolvedValue({ data: { key: 'macro.g7_unemployment~imf_weo', cell: { key: 'macro.g7_unemployment~imf_weo', status: 'bat', tracks: ['beat_macro'] }, rows: [WIN] } satisfies CellDetail as CellDetail });
  api.getTrack.mockResolvedValue({ data: { track: { id: 'beat_macro', label_fr: 'BEAT-MACRO — nowcasts', agent: 'BEAT-MACRO', version: 'V7', summary_fr: 'Même date.', report: null, report_expected: 'docs/dev/v7_beat_macro.md', protocol: { file: 'registry/forecasts/beat_macro/protocol.yaml', sha256: 'b'.repeat(64) }, files: [] }, counts: counts(1, 1, 0, 0), rows: [WIN, LOSS], holm_families: [{ window: 'holdout', size: 7, rows: 2 }], charts: [{ kind: 'ratio_by', title_fr: 'Ratio selon le mois', x_label_fr: 'mois', points: [{ x: 1, y: 0.8, n: 10 }, { x: 2, y: 0.7, n: 10 }] }], calendar: [] } as unknown as TrackDetail });
  api.getDoc.mockResolvedValue({ data: { path: 'registry/forecasts/beat_macro/protocol.yaml', sha256: 'c'.repeat(64), text: 'schema: x', format: 'yaml' } });
});
afterEach(() => vi.clearAllMocks());

describe('ScoresPage', () => {
  it('answers first: honest counts, every win with its caveats, weak references counted apart', async () => {
    renderAt('/v8/scores');
    const h = await screen.findByTestId('headline');
    expect(h).toHaveTextContent('REVA bat 1 · ne bat pas 1 · égalité 0 · en attente 1');
    expect(h).toHaveTextContent('Combinaison officiel + nowcast REVA bat point WEO en vigueur à t');
    expect(within(h).getByText('×0,63')).toBeInTheDocument();
    expect(within(h).getByText('utilise la prévision officielle')).toBeInTheDocument();
    expect(within(h).getByText('avantage de fraîcheur')).toBeInTheDocument();
    expect(h).toHaveTextContent('3 « victoires » V6 contre un repère faible');
  });

  it('matrix: no-track cells say why; a cell opens its rows (URL state) and a row opens its source', async () => {
    renderAt('/v8/scores');
    const none = await screen.findByTestId('cell-agri.wasde~usda_wasde');
    expect(none).toHaveTextContent('pas encore de piste');
    fireEvent.click(none);
    expect(await screen.findByTestId('cell-drawer')).toHaveTextContent('Piste BEAT-AGRI annoncée');
    fireEvent.click(screen.getByTestId('cell-macro.g7_unemployment~imf_weo'));
    await waitFor(() => expect(screen.getByTestId('where')).toHaveTextContent('cell=macro.g7_unemployment%7Eimf_weo'));
    const drawer = await screen.findByTestId('cell-drawer');
    await waitFor(() => expect(api.getCell).toHaveBeenCalledWith('macro.g7_unemployment~imf_weo', expect.anything()));
    fireEvent.click(await within(drawer).findByRole('button', { name: /Combinaison officiel/ }));
    const detail = await within(drawer).findByTestId('row-detail');
    expect(detail).toHaveTextContent('registry/forecasts/beat_macro/holdout.json');
    expect(detail).toHaveTextContent('#/results/1');
    expect(detail).toHaveTextContent('Utilise la prévision officielle en entrée.');
    act(() => {
      window.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape' }));
    });
    await waitFor(() => expect(screen.queryByTestId('cell-drawer')).toBeNull());
  });

  it('track view (?track=): wins and losses, Holm family, chart, protocol in the reader', async () => {
    renderAt('/v8/scores?track=beat_macro');
    const v = await screen.findByTestId('track-view');
    expect(v).toHaveTextContent('BEAT-MACRO — nowcasts');
    expect(v).toHaveTextContent('holdout m = 7');
    expect(v).toHaveTextContent('rapport attendu (docs/dev/v7_beat_macro.md) pas encore publié');
    expect(within(v).getAllByText('bat').length).toBeGreaterThan(0);
    expect(within(v).getByText('ne bat pas')).toBeInTheDocument();
    expect(screen.getByTestId('ratio-chart')).toBeInTheDocument();
    fireEvent.click(within(v).getByRole('button', { name: /protocole/ }));
    expect(await screen.findByRole('dialog')).toHaveTextContent('schema: x');
  });

  it('calendar: pre-registered tests and live registry dates; nothing resolved is said plainly', async () => {
    renderAt('/v8/scores');
    const cal = await screen.findByTestId('calendar');
    expect(cal).toHaveTextContent('22 oct. 2026');
    expect(cal).toHaveTextContent('3 juil. 2027');
    expect(await screen.findByText(/Aucune question résolue/)).toBeInTheDocument();
    expect(await screen.findByText(/53 questions — Hospitalisations grippe/)).toBeInTheDocument();
  });

  it('a cold live state shows counts it knows and no invented resolution counts', async () => {
    api.getLive.mockResolvedValue({ data: { ...LIVE_FULL, totals: { sealed_batches: 16, questions: 3610, forecasts: 14643 }, upcoming: [], partial: true, refreshing: true } });
    renderAt('/v8/scores');
    expect(await screen.findByText(/Calcul complet en cours/)).toBeInTheDocument();
    expect(screen.queryByText(/Aucune question résolue/)).toBeNull();
    expect(screen.getByText(/manquant/)).toBeInTheDocument();
  });

  it('forecaster focus (?f=) narrows the matrix to one column with its own count', async () => {
    renderAt('/v8/scores?f=imf_weo');
    const f = await screen.findByTestId('forecaster-focus');
    expect(f).toHaveTextContent('REVA contre FMI — WEO : 1 bat · 1 ne bat pas · 0 égalité · 1 en attente');
    expect(screen.queryByTestId('cell-agri.wasde~usda_wasde')).toBeNull();
  });
});
