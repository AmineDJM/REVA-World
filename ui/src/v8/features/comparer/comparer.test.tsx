import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { MemoryRouter, Route, Routes, useLocation } from 'react-router';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import type { MultiResponse, MultiSeries, Point } from '../monde/api';
import { buildCsv, CSV_COLUMNS, csvCell } from './csv';
import { compareHref, feature } from './index';
import { parseCompareQuery } from './parse';
import { allowed, commonBase, popScale, previousYearCode, transformPlace } from './transforms';

const LIC = { source: 'wb', licence: 'CC-BY-4.0', commercial_use: 'permitted', non_commercial: false, share_alike: false, no_derivatives: false, open_licence: true, restricted: false, basis: 'table_artifact' };
const P = (p: string, v: string | null, extra: Partial<Point> = {}): Point =>
  ({ p, ps: `${p}-01-01`, pe: `${Number(p) + 1}-01-01`, basis: 'calendar_year', v, t: null, unit: 'Current US$', proj: false, miss: v === null ? 'not_reported' : null, flags: {}, geo: 'FRA', iso3: 'FRA', series: 'wb.wdi_econ.NY.GDP.MKTP.CD', raw: 'r'.repeat(64), table_sha: null, lic: 0, ...extra }) as Point;

function meta(series: string, unit: string, label: string) {
  return {
    series,
    table: series.split('.').slice(0, 2).join('.'),
    source: series.split('.')[0]!,
    dataset: series.split('.')[1]!,
    indicator: series.split('.').slice(2).join('.'),
    measure: series.split('.').slice(2).join('.'),
    filters: [],
    concept: null,
    label,
    unit,
    units: [],
    value_kind: 'numeric',
    min_value: 1,
    precisions: { year: 4 },
    default_precision: 'year',
    countries: ['FRA', 'DEU'],
    levels: { adm0: 2, adm1: 0 },
    aggregates: [],
    recorded: null,
    licences: [LIC],
    transforms: {},
  };
}

const GDP: MultiSeries = {
  meta: meta('wb.wdi_econ.NY.GDP.MKTP.CD', 'Current US$', 'PIB'),
  precision: 'year',
  places: {
    FRA: [P('2020', '100'), P('2021', '110'), P('2022', null), P('2023', '130', { proj: true })],
    DEU: [P('2020', '200', { iso3: 'DEU' }), P('2021', '210', { iso3: 'DEU' }), P('2022', '231', { iso3: 'DEU' })],
  },
  transforms: { index: { ok: true, reason: null }, yoy: { ok: true, reason: null }, percap: { ok: true, reason: null } },
  vintages_available: false,
} as unknown as MultiSeries;
const CPI: MultiSeries = {
  ...GDP,
  meta: meta('imf.weo.PCPIPCH', '% change', 'Inflation'),
  places: { FRA: [P('2021', '2.1'), P('2022', '5.9')], DEU: [P('2021', '3.2', { iso3: 'DEU' }), P('2022', '8.7', { iso3: 'DEU' })] },
  transforms: { index: { ok: false, reason: 'unité en pourcentage' }, yoy: { ok: false, reason: 'déjà un taux' }, percap: { ok: false, reason: 'unité non additive' } },
} as unknown as MultiSeries;

const MULTI = {
  data: {
    series: [GDP, CPI],
    geos: ['FRA', 'DEU'],
    names: { FRA: 'France', DEU: 'Germany' },
    licences: [LIC],
    population: {
      concept: { id: 'reva.population.total', labels: { fr: 'Population totale' }, unit: 'persons' },
      places: { FRA: [{ year: 2020, v: '67000000', unit: 'persons', series: 'un_wpp.wpp2024_demo.TPopulation1July', method: 'reported' }], DEU: [] },
      licences: [],
    },
  },
  snapshot: { snapshot_sha256: 'a'.repeat(64), origin: 'live', knowledge_cutoff: '2026-10-17' },
  warnings: [],
  took_ms: 30,
} as unknown as MultiResponse;

// ------------------------------------------------------------------ pure

describe('transforms', () => {
  it('index base 100 at the first period common to every place', () => {
    expect(commonBase(GDP.places)).toBe('2020');
    const s = transformPlace(GDP.places.FRA!, 'index', { base: '2020' });
    expect(s.map((x) => x.y)).toEqual([100, 110, null, 130]);
    expect(s[2]!.miss).toBe('not_reported'); // missing stays missing, never 0
    expect(s[3]!.proj).toBe(true);
  });
  it('year-on-year change uses the same sub-period one year earlier', () => {
    expect(previousYearCode('2020-Q3')).toBe('2019-Q3');
    expect(previousYearCode('2020-03')).toBe('2019-03');
    const s = transformPlace(GDP.places.DEU!, 'yoy', {});
    expect(s[0]!.y).toBeNull();
    expect(s[0]!.miss).toBe('pas de valeur un an plus tôt');
    expect(s[2]!.y).toBeCloseTo(10);
    const proj = transformPlace(GDP.places.FRA!, 'yoy', {});
    expect(proj[3]!.miss).toBe('pas de valeur un an plus tôt'); // 2022 missing → no fabricated change
  });
  it('per capita divides by the population of the same year, missing otherwise', () => {
    const s = transformPlace(GDP.places.FRA!, 'percap', { pop: [{ year: 2020, v: '67000', series: 'un_wpp.x' }], popScale: 1000 });
    expect(s[0]!.y).toBeCloseTo(100 / 67_000_000);
    expect(s[0]!.note).toContain('un_wpp.x');
    expect(s[1]!.y).toBeNull();
    expect(s[1]!.miss).toBe('population manquante pour cette année');
    expect(popScale('thousands')).toBe(1000);
    expect(popScale('persons')).toBe(1);
  });
  it('allowed() follows the server rule and needs a common base for the index', () => {
    expect(allowed(CPI, 'index')).toEqual({ ok: false, reason: 'unité en pourcentage' });
    expect(allowed(GDP, 'index').ok).toBe(true);
    const noCommon = { ...GDP, places: { FRA: [P('2020', '1')], DEU: [P('2021', '2', { iso3: 'DEU' })] } } as unknown as MultiSeries;
    expect(allowed(noCommon, 'index').reason).toContain('aucune période');
  });
});

describe('palette parsing', () => {
  const places = [
    { iso3: 'FRA', label: 'France' },
    { iso3: 'DEU', label: 'Germany', frName: 'Allemagne' },
    { iso3: 'USA', label: 'United States', frName: 'États-Unis' },
    { iso3: 'AND', label: 'Andorra' },
    { iso3: 'CAN', label: 'Canada' },
  ];
  it('splits ISO3 codes and names from the indicator words', () => {
    expect(parseCompareQuery('comparer FRA DEU inflation', places)).toEqual({ geos: ['FRA', 'DEU'], rest: 'inflation' });
    expect(parseCompareQuery('allemagne et etats-unis pib par habitant', places)).toEqual({ geos: ['DEU', 'USA'], rest: 'pib par habitant' });
    expect(parseCompareQuery('fra vs usa chômage', places)).toEqual({ geos: ['FRA', 'USA'], rest: 'chômage' });
    // ambiguous words stay words unless typed as codes
    expect(parseCompareQuery('AND can inflation', places)).toEqual({ geos: ['AND'], rest: 'can inflation' });
  });
  it('the command builds the Comparer URL', () => {
    expect(compareHref('comparer FRA DEU inflation')).toBe('/v8/comparer?q=FRA+DEU+inflation');
    expect(compareHref('comparer')).toBe('/v8/comparer');
    expect([feature.id, feature.path, feature.order, feature.goKey]).toEqual(['comparer', 'comparer', 30, 'c']);
    expect(feature.commands![0]!.run('comparer FRA DEU inflation')).toBe('/v8/comparer?q=FRA+DEU+inflation');
  });
});

describe('csv', () => {
  it('exports exactly the shown values with source columns', () => {
    const shown = { FRA: transformPlace(GDP.places.FRA!, 'none', {}), DEU: transformPlace(GDP.places.DEU!, 'none', {}) };
    const csv = buildCsv([{ series: GDP, transform: 'none', unit: 'Current US$', shown, licences: [LIC as never], vintage: null }], ['FRA', 'DEU'], (g) => g, null);
    const lines = csv.trim().split('\r\n');
    expect(lines[0]).toBe(CSV_COLUMNS.join(','));
    expect(lines).toHaveLength(1 + 4 + 3);
    const fra22 = lines.find((l) => l.includes(',FRA,FRA,2022,'))!;
    expect(fra22).toContain(',not_reported,'); // missing reason, empty value — never 0
    expect(fra22.split(',')[7]).toBe('');
    const fra23 = lines.find((l) => l.includes(',FRA,FRA,2023,'))!;
    expect(fra23).toContain(',true,'); // projection flagged
    expect(fra23).toContain('CC-BY-4.0');
    expect(csvCell('a,"b"')).toBe('"a,""b"""');
  });
});

// ------------------------------------------------------------------ page

const api = vi.hoisted(() => ({ getMulti: vi.fn(), getVintages: vi.fn(), getCatalog: vi.fn(), getPlaces: vi.fn(), getMeasure: vi.fn(), getTable: vi.fn() }));
vi.mock('../monde/api', async (orig) => ({ ...(await orig<typeof import('../monde/api')>()), ...api }));

function Loc() {
  const l = useLocation();
  return <output data-testid="loc">{decodeURIComponent(l.search)}</output>;
}

async function renderPage(url: string) {
  const { default: ComparerPage } = await import('./ComparerPage');
  return render(
    <MemoryRouter initialEntries={[url]}>
      <Routes>
        <Route
          path="/v8/comparer"
          element={
            <>
              <ComparerPage />
              <Loc />
            </>
          }
        />
      </Routes>
    </MemoryRouter>,
  );
}

beforeEach(() => {
  Object.values(api).forEach((f) => f.mockReset());
  api.getMulti.mockResolvedValue(MULTI);
  api.getPlaces.mockResolvedValue([
    { iso3: 'FRA', iso2: 'FR', label: 'France', label_fr: null, region: 'Europe' },
    { iso3: 'DEU', iso2: 'DE', label: 'Germany', label_fr: null, region: 'Europe' },
  ]);
});

describe('ComparerPage', () => {
  it('explains how to start without inventing anything', async () => {
    await renderPage('/v8/comparer');
    expect(screen.getByText('Ajoutez des pays')).toBeInTheDocument();
    expect(api.getMulti).not.toHaveBeenCalled();
  });

  it('draws one panel per indicator; transforms are offered only when the inputs allow them', async () => {
    await renderPage('/v8/comparer?g=FRA,DEU&s=wb.wdi_econ.NY.GDP.MKTP.CD&s=imf.weo.PCPIPCH');
    await screen.findByRole('heading', { name: 'PIB' });
    expect(api.getMulti).toHaveBeenCalledWith(['wb.wdi_econ.NY.GDP.MKTP.CD', 'imf.weo.PCPIPCH'], ['FRA', 'DEU'], false);
    const base = screen.getByRole('button', { name: 'Base 100' });
    expect(base).not.toBeDisabled();
    fireEvent.click(base);
    await waitFor(() => expect(screen.getByTestId('loc').textContent).toContain('t=index'));
    // inflation cannot be indexed: shown untransformed with the reason
    expect(await screen.findByText(/Non transformé .*unité en pourcentage/)).toBeInTheDocument();
  });

  it('per capita is labelled a REVA computation naming the population source', async () => {
    await renderPage('/v8/comparer?g=FRA,DEU&s=wb.wdi_econ.NY.GDP.MKTP.CD&t=percap');
    const note = await screen.findByTestId('percap-note');
    expect(note.textContent).toContain('calcul REVA');
    expect(note.textContent).toContain('un_wpp.wpp2024_demo.TPopulation1July');
    expect(api.getMulti).toHaveBeenCalledWith(['wb.wdi_econ.NY.GDP.MKTP.CD'], ['FRA', 'DEU'], true);
  });

  it('table view shows missing cells as missing, never 0', async () => {
    await renderPage('/v8/comparer?g=FRA,DEU&s=wb.wdi_econ.NY.GDP.MKTP.CD&vue=tableau');
    const panel = await screen.findByRole('region', { name: 'PIB' });
    const row = within(panel).getByRole('rowheader', { name: '2022' }).closest('tr')!;
    expect(within(row).getByTestId('v8-missing')).toBeInTheDocument();
    expect(row.textContent).toContain('231');
    expect(within(panel).getByText(/130/).textContent).toContain('ᴾ');
  });

  it('resolves a palette query: places from codes or names, indicator from the catalog', async () => {
    api.getCatalog.mockResolvedValue({ query: 'inflation', concepts_total: 1, themes: {}, hits: [{ kind: 'concept', id: 'imf.cpi', label: 'Inflation', table: 'imf.weo', measure: 'PCPIPCH', filters: [], series: 'imf.weo.PCPIPCH', in_snapshot: true }] });
    await renderPage('/v8/comparer?q=FRA%20allemagne%20inflation');
    await waitFor(() => expect(screen.getByTestId('loc').textContent).toBe('?g=FRA,DEU&s=imf.weo.PCPIPCH'));
    expect(api.getCatalog).toHaveBeenCalledWith('inflation', null);
  });
});

describe('alignment', () => {
  it('annual points sit on their year code, whatever the fiscal start', async () => {
    const { xOfPoint } = await import('./transforms');
    expect(xOfPoint({ p: '2020', ps: '2020-07-01' })).toBe(2020);
    expect(xOfPoint({ p: '2020-Q3', ps: '2020-07-01' })).toBeCloseTo(2020.5);
  });
});
