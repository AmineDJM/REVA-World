import { configure, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter, Route, Routes, useLocation } from 'react-router';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { mockFetch, type Call } from '../../../test/fetchMock';
import { writeLocalBoards } from '../tableaux/local';
import { addItem, emptyBoard } from '../tableaux/model';
import CHANGES from './__fixtures__/changes_recorded.json';
import DECISIONS from './__fixtures__/coverage_decisions.json';
import CALENDAR from './__fixtures__/scores_calendar.json';
import LIVE from './__fixtures__/scores_live.json';
import AccueilPage from './AccueilPage';
import { getCalendar, getCoverage, getDecisions, shared, type ChangeItem } from './api';
import { feature } from './index';
import { countsFr, daysUntil, groupByDay, relDays, sinceFor, verdictOf } from './model';

configure({ asyncUtilTimeout: 4000 });
vi.setConfig({ testTimeout: 20_000 });

const NO_ROUTE = { status: 404, body: { title: 'not found', status: 404, detail: 'no such endpoint', code: 'not_found' } };

/** Shape of CoverageMeta (src/reva/api/routes/v8_coverage.py); atlas numbers from docs/dev/HANDOFF_V3.md (snapshot 717e8452…). */
const COVERAGE = {
  level: 'region',
  meta: { computed_at: '2026-10-17T20:00:00Z', today: '2026-10-17', origin: 'live', atlas_tables: 221, atlas_rows: 93495663, kg_entities: 182000, kg_statements: 2680000, kg_computed: true, places: 251, stale: null, note: '', refresh_command: '' },
  columns: [],
  rows: [],
};

function Loc() {
  const l = useLocation();
  return <output data-testid="loc">{l.pathname + l.search}</output>;
}

function renderAt(url = '/v8') {
  return render(
    <MemoryRouter initialEntries={[url]}>
      <Routes>
        <Route
          path="/v8"
          element={
            <>
              <AccueilPage />
              <Loc />
            </>
          }
        />
        <Route path="*" element={<Loc />} />
      </Routes>
    </MemoryRouter>,
  );
}

function routes(over: Record<string, unknown> = {}) {
  return mockFetch({
    'GET /v1/v8/changes': { body: CHANGES },
    'GET /v1/v8/scores/live': { body: LIVE },
    'GET /v1/v8/scores/calendar': { body: CALENDAR },
    'GET /v1/v8/coverage/decisions': { body: DECISIONS },
    'GET /v1/v8/coverage/matrix': { body: COVERAGE },
    'GET /v1/v8/boards': NO_ROUTE,
    ...(over as Record<string, { status?: number; body?: unknown }>),
  });
}

beforeEach(() => {
  window.localStorage.clear();
  vi.useFakeTimers({ toFake: ['Date'] });
  vi.setSystemTime(new Date('2026-10-17T23:00:00Z'));
});
afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

// reset the per-page shared requests between tests
afterEach(() => {
  for (const f of [getCalendar, getDecisions, getCoverage]) f.reset();
});

describe('accueil model', () => {
  it('formats counts in French, omitting zeros', () => {
    expect(countsFr({ rows: 30187, tables: 1, revisions: 0 })).toBe('30 187 lignes · 1 table');
    expect(countsFr({ added: 1, retired: 0 })).toBe('1 ajoutée');
    expect(countsFr({ inconnu: 2 })).toBe('2 inconnu');
  });

  it('groups by day and computes windows', () => {
    const items = (CHANGES.data.items as unknown as ChangeItem[]).slice(0, 3);
    const g = groupByDay(items, new Date('2026-10-18T09:00:00'));
    expect(g[0]!.day).toBe('Hier');
    expect(sinceFor('24h', Date.parse('2026-10-17T23:00:30Z'))).toBe('2026-10-16T23:00:00.000Z');
    expect(daysUntil('2026-10-18', new Date('2026-10-17T10:00:00'))).toBe(1);
    expect(relDays(1)).toBe('demain');
    expect(relDays(-3)).toBe('il y a 3 j');
  });

  it('states the judged status of a resolution only as the feed gives it', () => {
    const voided = (CHANGES.data.items as unknown as ChangeItem[]).find((i) => i.kind === 'question_resolved')!;
    expect(verdictOf({ ...voided, detail: { ...voided.detail } })).toEqual({ text: 'annulée : fiscal year basis mismatch', kind: 'missing' });
    const base = { ...voided, summary: null, detail: { status: 'resolved', official: 'absent' } } as ChangeItem;
    expect(verdictOf(base)?.text).toMatch(/aucun prévisionniste officiel/);
    expect(verdictOf({ ...base, detail: { status: 'resolved', official: { 'forecast.reference_imf_weo': {} } } })).toEqual({
      text: expect.stringMatching(/comparée à forecast.reference_imf_weo — une question n’est pas un verdict/),
      kind: 'neutral',
    });
    expect(verdictOf({ ...base, detail: { verdict_fr: 'ne bat pas le FMI' } })?.kind).toBe('loss');
    expect(verdictOf({ ...base, kind: 'ingest' })).toBeNull();
  });

  it('shares one request between panels and forgets failures', async () => {
    let n = 0;
    const f = shared(async () => {
      n += 1;
      if (n === 1) throw new Error('x');
      return n;
    });
    await expect(f()).rejects.toThrow();
    expect(await f()).toBe(2);
    expect(await f()).toBe(2);
  });
});

describe('AccueilPage', () => {
  it('answers "what changed / what needs me" with real recorded data', async () => {
    const { calls } = routes();
    renderAt();
    // glance strip
    const glance = screen.getByRole('region', { name: 'Couverture en bref' });
    expect(await within(glance).findByText('93,5 M')).toBeInTheDocument();
    expect(within(glance).getByText(/221 tables · 251 lieux/)).toBeInTheDocument();
    await waitFor(() => expect(glance).toHaveTextContent(/5.950/)).catch((e) => {
      throw new Error(`GLANCE: ${glance.textContent} || CAL: ${screen.getByRole('region', { name: 'Prochaines résolutions' }).textContent}`, { cause: e });
    }); // sealed questions from /scores/live
    expect(within(glance).getByText('65')).toBeInTheDocument(); // open owner decisions
    // change feed: rows with French counts, links, void resolutions labelled
    const feed = screen.getByRole('region', { name: 'Ce qui a changé' });
    expect(await within(feed).findByText(/ClinicalTrials.gov API v2/)).toBeInTheDocument();
    expect(within(feed).getAllByText(/38 lignes · 1 table/)[0]).toBeInTheDocument();
    expect(within(feed).getAllByText(/annulée : fiscal year basis mismatch/).length).toBeGreaterThan(0);
    expect(within(feed).getAllByText(/Nombres attribués à leur source/).length).toBeGreaterThan(0);
    // the default request leaves owner decisions to their own panel
    const req = calls.find((c: Call) => c.url.startsWith('/v1/v8/changes'))!;
    expect(new URL(req.url, 'http://x').searchParams.get('kinds')).not.toContain('owner_decision');
    // decisions panel
    const dec = screen.getByRole('region', { name: 'Décisions qui vous attendent' });
    expect(await within(dec).findByText('Clé API openFDA')).toBeInTheDocument();
    expect(within(dec).getByText(/Débloque : Comptes FAERS/)).toBeInTheDocument();
    expect(within(dec).getByRole('link', { name: /\+ 11 autres/ })).toBeInTheDocument(); // 15 open in the fixture, 4 shown
    // calendar panel: next resolution group with its official forecaster
    const cal = screen.getByRole('region', { name: 'Prochaines résolutions' });
    expect((await within(cal).findAllByText(/Hospitalisations COVID-19/))[0]).toBeInTheDocument();
    expect(within(cal).getAllByText(/contre cdc_covidhub/)[0]).toBeInTheDocument();
    expect(within(cal).getByText(/en retard de résolution/)).toBeInTheDocument();
    // boards: local mode, honest empty state
    const boards = screen.getByRole('region', { name: 'Tableaux épinglés' });
    expect(await within(boards).findByText('Aucun tableau')).toBeInTheDocument();
  });

  it('keeps the window and the kind filter in the URL', async () => {
    const { calls } = routes();
    renderAt();
    const feed = screen.getByRole('region', { name: 'Ce qui a changé' });
    await within(feed).findByText(/ClinicalTrials.gov API v2/);
    await userEvent.click(within(feed).getByRole('radio', { name: '7 jours' }));
    expect(screen.getByTestId('loc')).toHaveTextContent('/v8?p=7j');
    await userEvent.click(await within(feed).findByRole('button', { name: /^Données/ }));
    expect(screen.getByTestId('loc')).toHaveTextContent('/v8?p=7j&k=ingest');
    await waitFor(() => expect(calls.some((c) => c.url.includes('kinds=ingest'))).toBe(true));
    // other chips stay (counts of the unfiltered request)
    expect(within(feed).getByRole('button', { name: /^Graphe/ })).toBeInTheDocument();
  });

  it('is honest when the change feed and the coverage are not deployed', async () => {
    routes({ 'GET /v1/v8/changes': NO_ROUTE, 'GET /v1/v8/coverage/matrix': NO_ROUTE, 'GET /v1/v8/coverage/decisions': NO_ROUTE });
    renderAt();
    expect(await screen.findByText('Fil des changements pas encore disponible')).toBeInTheDocument();
    expect(screen.getByText('Liste des décisions pas encore disponible')).toBeInTheDocument();
    const glance = screen.getByRole('region', { name: 'Couverture en bref' });
    await waitFor(() => expect(within(glance).getAllByText('indisponible').length).toBeGreaterThanOrEqual(2));
    expect(within(glance).queryByText('0')).toBeNull(); // never a zero for a missing number
  });

  it('says so when nothing changed, and offers a wider window', async () => {
    routes({ 'GET /v1/v8/changes': { body: { data: { since: 'x', until: 'y', items: [], counts: {} } } } });
    renderAt('/v8?p=24h');
    expect(await screen.findByText('Aucun changement enregistré sur 24 h')).toBeInTheDocument();
    await userEvent.click(screen.getByRole('button', { name: 'Élargir à 7 jours' }));
    expect(screen.getByTestId('loc')).toHaveTextContent('p=7j');
  });

  it('falls back to the forecast registry when the scores service is absent, and labels the sample', async () => {
    routes({
      'GET /v1/v8/scores/live': NO_ROUTE,
      'GET /v1/v8/scores/calendar': NO_ROUTE,
      'GET /v1/forecasts/questions': {
        body: {
          data: {
            total: 2393,
            offset: 0,
            questions: [
              { question_id: 'fq_6e3950bd6ff92362b83f20ae', cycle: '2026-Q4', family: 'ucdp.sb_deaths_cy', label_fr: 'Morts au combat (UCDP)', geo: 'COL', geo_label_fr: 'Colombia', reference_period: '2026', type: 'quantity', unit: 'deaths', threshold: null, text_fr: '…', expected_resolution: '2027-06-30', void_after: '2028-10-02', resolution_source: null, status: 'pending', outcome: null, n_forecasts: 1, batch_sha256: 'x', issued_at: null },
            ],
          },
        },
      },
    });
    renderAt();
    const cal = screen.getByRole('region', { name: 'Prochaines résolutions' });
    await waitFor(() => expect(cal).toHaveTextContent(/aperçu sur les 1 premières questions en attente \(sur 2.393\)/));
    expect(within(cal).getByText('Morts au combat (UCDP)')).toBeInTheDocument();
  });

  it('previews pinned boards and navigates with the keyboard', async () => {
    routes();
    let b = emptyBoard('Énergie');
    b = addItem(b, { kind: 'note', markdown: 'x' }).board;
    writeLocalBoards([b]);
    renderAt();
    const boards = screen.getByRole('region', { name: 'Tableaux épinglés' });
    expect(await within(boards).findByRole('link', { name: /Énergie/ })).toHaveAttribute('href', `/v8/tableaux/${b.id}`);
    const feed = screen.getByRole('region', { name: 'Ce qui a changé' });
    await within(feed).findByText(/ClinicalTrials.gov API v2/);
    fireEvent.keyDown(window, { key: 'j' });
    const first = feed.querySelector('.v8a-change') as HTMLElement;
    expect(first).toHaveFocus();
    fireEvent.keyDown(window, { key: 'j' });
    expect(feed.querySelectorAll('.v8a-change')[1]).toHaveFocus();
    fireEvent.keyDown(document.activeElement!, { key: 'Enter' });
    expect(screen.getByTestId('loc').textContent).toMatch(/^\/v8\/(monde|fiche)/);
  });

  it('q focuses the question box and Enter asks', async () => {
    routes();
    renderAt();
    fireEvent.keyDown(window, { key: 'q' });
    const box = screen.getByLabelText('Poser une question');
    expect(box).toHaveFocus();
    await userEvent.type(box, 'inflation France{Enter}');
    expect(screen.getByTestId('loc')).toHaveTextContent('/v8/demander?q=inflation+France');
  });
});

describe('feature contract', () => {
  it('is the /v8 index with goKey h and palette commands', () => {
    expect(feature).toMatchObject({ id: 'accueil', path: '', order: 0, goKey: 'h' });
    expect(feature.commands!.map((c) => c.run(''))).toContain('/v8?p=30j&k=question_resolved');
  });
});
