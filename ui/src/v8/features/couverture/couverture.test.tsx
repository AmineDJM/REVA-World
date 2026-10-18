import { describe, expect, it, beforeEach } from 'vitest';
import { render, screen, fireEvent, within, waitFor } from '@testing-library/react';
import { MemoryRouter, Route, Routes, useLocation } from 'react-router';
import { mockFetch } from '../../../test/fetchMock';
import CouverturePage from './CouverturePage';
import { clearCoverageCache } from './api';
import { cellText, cellTitle, frYear, intensity } from './model';
import { feature } from './index';

// SYNTHETIC fixtures (shapes of /v1/v8/coverage/*): two domains, two regions — not real counts.
const meta = {
  computed_at: '2026-10-17T22:36:00+00:00',
  today: '2026-10-17',
  origin: 'live',
  atlas_tables: 3,
  atlas_rows: 42,
  kg_entities: 4,
  kg_statements: 9,
  kg_computed: true,
  places: 3,
  stale: { tables_registered_since: 2, last_registered_at: '2026-10-17T23:00:00+00:00' },
  note: 'Couverture = ce que REVA a chargé, pas la réalité',
  refresh_command: 'uv run reva v8 coverage refresh',
};
const summary = (over: Record<string, unknown> = {}) => ({
  tables: 2,
  rows: 35,
  countries: 2,
  latest_year: 2025,
  last_loaded_at: '2026-10-16T10:00:00+00:00',
  licences: { open: 1, non_commercial: 1 },
  kg_entities: 0,
  catalog: null,
  pending_loads: 1,
  pending_decisions: 1,
  ...over,
});
const matrix = {
  level: 'region',
  meta,
  columns: [
    { id: 'Africa', label: 'Afrique', region: 'Africa', countries: 1 },
    { id: 'Europe', label: 'Europe', region: 'Europe', countries: 2 },
  ],
  rows: [
    { id: 'macro', label: 'Macroéconomie et travail', summary: summary(), cells: { Europe: { countries_with_rows: 2, rows: 35, tables: 2, y1: 2025, y1_median: 2025 } } },
    { id: 'biology', label: 'Biologie', summary: summary({ tables: 0, rows: 0, countries: 0, latest_year: null, licences: {}, kg_entities: 4 }), cells: {} },
  ],
};
const item = (over: Record<string, unknown>) => ({
  id: 'x',
  type: 'decision',
  kind: 'licence',
  kind_label: 'Licences',
  title: 'Une décision',
  detail: null,
  unlocks: { domains: ['macro'], atlas_sources: [], tables: [], tracks: [], text: 'quelque chose' },
  action: 'Décider',
  commands: [],
  who: 'owner',
  status: 'open',
  disk_mb: null,
  source_docs: ['docs/dev/HANDOFF_V3.md'],
  observed: null,
  ...over,
});
const decisions = {
  type: 'decision',
  updated: '2026-10-17',
  checked_at: null,
  counts: { items: 2, open: 2, observed_done: 0 },
  disk_mb_open_loads: 0,
  disk_mb_unknown: 0,
  groups: [
    { kind: 'licence', label: 'Licences', items: [item({ id: 'licence.imf', title: 'Conditions FMI' })] },
    { kind: 'account_key', label: 'Comptes et clés', items: [item({ id: 'key.fred', kind: 'account_key', title: 'Clé FRED' })] },
  ],
};
const pending = {
  type: 'load',
  updated: '2026-10-17',
  checked_at: '2026-10-17T23:04:00+00:00',
  counts: { items: 2, open: 2, observed_done: 1 },
  disk_mb_open_loads: 55,
  disk_mb_unknown: 0,
  groups: [
    {
      kind: 'disk',
      label: 'Disque (≥ 5 Go libres)',
      items: [
        item({ id: 'load.a', type: 'load', kind: 'disk', title: 'Charger A', who: 'lead', status: 'queued', disk_mb: 55, commands: ['flock /tmp/reva-devdb.lock uv run reva atlas ingest atlas.a'], observed: { state: 'not_seen', checks: [] } }),
        item({ id: 'load.b', type: 'load', kind: 'disk', title: 'Charger B', who: 'lead', status: 'queued', disk_mb: 5, commands: ['x'], observed: { state: 'done', checks: [{ connector: 'atlas.b', min_version: null, versions_acquired: ['1.0.0'], last_retrieved_at: '2026-10-17T22:00:00+00:00', tables_missing: [], met: true }] } }),
      ],
    },
  ],
};

function Where() {
  const l = useLocation();
  return <output data-testid="where">{l.pathname + l.search}</output>;
}

function renderAt(url: string) {
  return render(
    <MemoryRouter initialEntries={[url]}>
      <Routes>
        <Route
          path="/v8/couverture"
          element={
            <>
              <CouverturePage />
              <Where />
            </>
          }
        />
      </Routes>
    </MemoryRouter>,
  );
}

beforeEach(() => {
  clearCoverageCache();
  mockFetch({
    'GET /v1/v8/coverage/matrix': { body: matrix },
    'GET /v1/v8/coverage/decisions': { body: decisions },
    'GET /v1/v8/coverage/pending': { body: pending },
  });
});

describe('model', () => {
  const col = { id: 'Europe', label: 'Europe', region: 'Europe', countries: 2 };
  it('never shades an absent cell and words it as "nothing attached", not as zero', () => {
    expect(intensity(undefined, col, 'pays', 10, 2026)).toBeNull();
    expect(cellText(undefined, col, 'pays', 'region')).toBe('');
    expect(cellTitle('Santé', col, undefined, 'region')).toMatch(/rien de stocké.*pas l’absence du phénomène/);
    expect(intensity({ countries_with_rows: 1, rows: 3 }, col, 'pays', 10, 2026)).toBeCloseTo(0.5);
    expect(cellText({ countries_with_rows: 1, rows: 3 }, col, 'pays', 'region')).toBe('1/2');
  });
  it('a cell with only future-dated rows has no latest observed year', () => {
    expect(cellTitle('Population', col, { countries_with_rows: 1, rows: 3, tables: 1, y1: null, future_rows: 3 }, 'region')).toMatch(/seulement des périodes futures/);
  });
  it('formats astronomical years', () => {
    expect(frYear(-9999)).toBe('10 000 av. J.-C.');
    expect(frYear(0)).toBe('1 av. J.-C.');
    expect(frYear(2025)).toBe('2025');
  });
});

describe('Couverture page', () => {
  it('renders the matrix with hatched absent cells, the answer strip and the staleness', async () => {
    renderAt('/v8/couverture');
    const table = await screen.findByRole('table', { name: 'Couverture par domaine et par lieu' });
    expect(within(table).getByText('2/2')).toBeInTheDocument();
    const absent = within(table).getAllByRole('button', { name: /rien de stocké/ });
    expect(absent.length).toBe(3); // macro × Afrique, biology × Afrique and Europe
    expect(absent.every((b) => b.textContent === '')).toBe(true); // never "0"
    expect(screen.getByText(/2 tables chargées depuis/)).toBeInTheDocument();
    expect(await screen.findByText('Décisions à prendre')).toBeInTheDocument();
  });

  it('keeps every state in the URL: level, domain detail, Escape closes', async () => {
    renderAt('/v8/couverture');
    await screen.findByRole('table', { name: 'Couverture par domaine et par lieu' });
    fireEvent.click(screen.getByRole('radio', { name: 'Sous-régions' }));
    expect(screen.getByTestId('where').textContent).toContain('niveau=subregion');
    fireEvent.click(screen.getByRole('button', { name: /Macroéconomie et travail — détail/ }));
    expect(screen.getByTestId('where').textContent).toContain('domaine=macro');
    fireEvent.keyDown(window, { key: 'Escape' });
    await waitFor(() => expect(screen.getByTestId('where').textContent).not.toContain('domaine='));
  });

  it('moves between cells with the arrow keys', async () => {
    renderAt('/v8/couverture');
    const table = await screen.findByRole('table', { name: 'Couverture par domaine et par lieu' });
    const first = within(table).getByRole('button', { name: /Macroéconomie et travail — détail/ });
    first.focus();
    fireEvent.keyDown(first, { key: 'ArrowRight' });
    expect(document.activeElement?.getAttribute('aria-label')).toMatch(/Afrique/);
    fireEvent.keyDown(document.activeElement!, { key: 'ArrowDown' });
    expect(document.activeElement?.getAttribute('aria-label')).toMatch(/^Biologie · Afrique/);
  });

  it('lists owner decisions grouped by kind with what each unlocks', async () => {
    renderAt('/v8/couverture?vue=decisions');
    expect(await screen.findByText('Conditions FMI')).toBeInTheDocument();
    expect(screen.getByRole('heading', { name: 'Licences (1)' })).toBeInTheDocument();
    expect(screen.getByRole('heading', { name: 'Comptes et clés (1)' })).toBeInTheDocument();
    expect(screen.getAllByText(/Débloque :/).length).toBe(2);
    fireEvent.change(screen.getByRole('searchbox', { name: 'Filtrer la liste' }), { target: { value: 'FRED' } });
    expect(screen.queryByText('Conditions FMI')).not.toBeInTheDocument();
    expect(screen.getByTestId('where').textContent).toContain('q=FRED');
  });

  it('shows queued loads with the live check, copyable commands, executed ones last', async () => {
    renderAt('/v8/couverture?vue=chargements');
    const a = await screen.findByText('Charger A');
    const b = screen.getByText('Charger B');
    expect(a.compareDocumentPosition(b) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(screen.getByText('pas encore constaté')).toBeInTheDocument();
    expect(screen.getByText(/exécution constatée/)).toBeInTheDocument();
    expect(screen.getAllByRole('button', { name: 'Copier' }).length).toBeGreaterThan(0);
    expect(screen.getByText(/5 Go libres/, { selector: 'strong' })).toBeInTheDocument();
  });

  it('says why when the aggregates were never computed', async () => {
    clearCoverageCache();
    mockFetch({});
    renderAt('/v8/couverture');
    expect(await screen.findByText(/uv run reva v8 coverage refresh/, { selector: '.cv-note code' })).toBeInTheDocument();
  });
});

describe('feature contract', () => {
  it('declares route, order and go key; palette commands map to URLs', () => {
    expect(feature).toMatchObject({ id: 'couverture', path: 'couverture', order: 80, goKey: 'v' });
    const cmd = (id: string) => feature.commands!.find((c) => c.id === id)!;
    expect(cmd('couverture.profil').run('couverture fra')).toBe('/v8/couverture?pays=FRA');
    expect(cmd('couverture.profil').run('couverture')).toBe('/v8/couverture?niveau=country');
    expect(cmd('couverture.decisions').run('')).toBe('/v8/couverture?vue=decisions');
  });
  it('searches domains and decisions (API loaded lazily)', async () => {
    const hits = await feature.search!('fmi', new AbortController().signal);
    expect(hits.map((h) => h.label)).toContain('Conditions FMI');
    const dom = await feature.search!('macro', new AbortController().signal);
    expect(dom[0]?.href).toBe('/v8/couverture?domaine=macro');
  });
});
