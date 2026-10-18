import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { MemoryRouter, Route, Routes, useLocation } from 'react-router';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import type { MapResponse, PlaceResponse } from './api';
import { classColors, classify, classOf, colorOf, effectiveMode, legendRows, quantileBreaks } from './scale';
import { mondeHref, readState, selectorKey, writeSelector } from './url';
import { v8Links } from '../../contract';
import { feature } from './index';

// ------------------------------------------------------------------ fixtures (shapes of /v1/v8/series/*)

const LIC = { source: 'wb', licence: 'CC-BY-4.0', commercial_use: 'permitted', non_commercial: false, share_alike: false, no_derivatives: false, open_licence: true, restricted: false, basis: 'table_artifact' };
const META = {
  series: 'wb.wdi_econ.NY.GDP.PCAP.CD',
  table: 'wb.wdi_econ',
  source: 'wb',
  dataset: 'wdi_econ',
  indicator: 'NY.GDP.PCAP.CD',
  measure: 'NY.GDP.PCAP.CD',
  filters: [],
  concept: { id: 'wb.gdp_pc', labels: { fr: 'PIB par habitant' }, definition: 'GDP per capita.', unit: 'Current US$', theme: 'economy' },
  label: 'PIB par habitant',
  unit: 'Current US$',
  units: [{ unit: 'Current US$', cells: 10 }],
  value_kind: 'numeric',
  min_value: 100,
  precisions: { year: 10 },
  default_precision: 'year',
  countries: ['FRA', 'DEU', 'ITA'],
  levels: { adm0: 3, adm1: 0 },
  aggregates: ['WLD'],
  recorded: '2026-10-15',
  licences: [LIC],
  transforms: { index: { ok: true, reason: null }, yoy: { ok: true, reason: null }, percap: { ok: false, reason: 'déjà rapporté à la population' } },
};
const pt = (iso3: string, v: string | null, extra: Record<string, unknown> = {}) => ({
  p: '2022',
  ps: '2022-01-01',
  pe: '2023-01-01',
  basis: 'calendar_year',
  v,
  t: null,
  unit: 'Current US$',
  proj: false,
  miss: null,
  flags: {},
  geo: iso3,
  iso3,
  series: null,
  raw: 'a'.repeat(64),
  lic: 0,
  ...extra,
});
const MAP: MapResponse = {
  data: {
    meta: META,
    precision: 'year',
    periods: [
      { code: '2021', label: '2021', obs: 2, proj: 0 },
      { code: '2022', label: '2022', obs: 2, proj: 0 },
      { code: '2023', label: '2023', obs: 0, proj: 1 },
    ],
    period: '2022',
    period_label: '2022',
    level: 'adm0',
    projections: 'exclude',
    values: [pt('FRA', '40886.25'), pt('DEU', '48717.99')],
    missing: [
      { geo: 'ITA', reason: 'provider:not_reported' },
      { geo: 'ESP', reason: 'not_in_series' },
    ],
    missing_labels: { not_in_series: 'jamais rapporté par cette série (absent, pas zéro)', 'provider:not_reported': 'non rapporté par la source' },
    aggregates: [pt('WLD', '12687.7', { iso3: null })],
    licences: [LIC],
    scale_hint: 'quantile',
    counts: { values: 2, observed: 2, projection: 0, projection_hidden: 0, provider_missing: 1, missing: 2 },
    names: { FRA: 'France', DEU: 'Germany' },
  },
  snapshot: { snapshot_sha256: 'f'.repeat(64), origin: 'live', knowledge_cutoff: '2026-10-17T00:00:00Z' },
  notes: [],
  warnings: [],
  took_ms: 21,
} as unknown as MapResponse;

const PLACE: PlaceResponse = {
  data: {
    meta: META,
    geo: 'FRA',
    name: 'France',
    precision: 'year',
    points: [pt('FRA', '39000', { p: '2021', ps: '2021-01-01', pe: '2022-01-01' }), pt('FRA', '40886.25'), pt('FRA', '42000', { p: '2023', ps: '2023-01-01', pe: '2024-01-01', proj: true })],
    licences: [LIC],
  },
  snapshot: MAP.snapshot,
  warnings: [],
} as unknown as PlaceResponse;

const api = vi.hoisted(() => ({
  getMap: vi.fn(),
  getPlace: vi.fn(),
  getCatalog: vi.fn(),
  getMeasure: vi.fn(),
  getTable: vi.fn(),
  getPlaces: vi.fn(),
}));
vi.mock('./api', async (orig) => ({ ...(await orig<typeof import('./api')>()), ...api }));
vi.mock('./MondeMap', () => ({
  default: (p: { fills: Map<string, { state: string }>; onSelect: (iso: string) => void; renderTip: (iso: string, n: string) => unknown }) => (
    <div data-testid="monde-map">
      {[...p.fills.entries()].map(([iso, f]) => (
        <button key={iso} type="button" data-state={f.state} onClick={() => p.onSelect(iso)}>
          {iso}
        </button>
      ))}
      <div data-testid="tip-ITA">{p.renderTip('ITA', 'Italy') as never}</div>
      <div data-testid="tip-FRA">{p.renderTip('FRA', 'France') as never}</div>
    </div>
  ),
}));
vi.mock('../../../api/client', () => ({ getLocations: vi.fn(async () => ({ data: { type: 'FeatureCollection', features: [] } })) }));

function Loc() {
  const l = useLocation();
  return <output data-testid="loc">{l.search}</output>;
}

async function renderPage(url: string) {
  const { default: MondePage } = await import('./MondePage');
  return render(
    <MemoryRouter initialEntries={[url]}>
      <Routes>
        <Route
          path="/v8/monde"
          element={
            <>
              <MondePage />
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
  api.getMap.mockResolvedValue(MAP);
  api.getPlace.mockResolvedValue(PLACE);
  api.getPlaces.mockResolvedValue([
    { iso3: 'FRA', iso2: 'FR', label: 'France', label_fr: null, region: 'Europe' },
    { iso3: 'DEU', iso2: 'DE', label: 'Germany', label_fr: null, region: 'Europe' },
    { iso3: 'ITA', iso2: 'IT', label: 'Italy', label_fr: null, region: 'Europe' },
  ]);
  api.getMeasure.mockResolvedValue({ table: 'wb.wdi_econ', measure: 'NY.GDP.PCAP.CD', facets: [], variants: 1, resolved: { series: META.series, indicator: META.indicator } });
});

// ------------------------------------------------------------------ pure modules

describe('scale', () => {
  it('quantile breaks never duplicate and keep one class per value when few', () => {
    expect(quantileBreaks([1, 2, 3], 6)).toEqual([1, 2]);
    const b = quantileBreaks([1, 1, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10], 4);
    expect(new Set(b).size).toBe(b.length);
  });
  it('log needs positive values and diverging needs both signs (else quantile)', () => {
    expect(effectiveMode('log', 'quantile', [0, 1, 2])).toBe('quantile');
    expect(effectiveMode('diverging', 'quantile', [1, 2])).toBe('quantile');
    expect(effectiveMode('auto', 'log', [1, 1e5])).toBe('log');
    expect(effectiveMode('auto', 'diverging', [-1, 2])).toBe('diverging');
  });
  it('legend ranges show reported values and colours follow the theme', () => {
    const c = classify([1, 2, 3, 10, 20, 30, 100, 200], 'quantile');
    const light = classColors(c, false);
    const dark = classColors(c, true);
    expect(light).toHaveLength(c.n);
    expect(light[0]).not.toBe(dark[0]);
    const rows = legendRows(c, light, (x) => String(x));
    expect(rows[0]!.label.startsWith('1')).toBe(true);
    expect(rows.reduce((s, r) => s + r.count, 0)).toBe(8);
  });
  it('diverging is symmetric around zero with a neutral middle class', () => {
    const c = classify([-50, -10, 0, 5, 100], 'diverging');
    expect(c.breaks).toEqual([-50, -10, -5, 5, 10, 50]);
    expect(classOf(c, 100)).toBe(c.n - 1); // the extreme value sits in the extreme class
    expect(classColors(c, false)).toHaveLength(7);
    expect(classColors(c, false)[3]).toBe('#f0efec');
    expect(classOf(c, 0)).toBe(3);
  });

  it('diverging: one extreme value does not flatten the others into the neutral class (live « empty map »)', () => {
    // inflation-like slice: Venezuela 252 %, a deflation at −4.7 %, most places between 1 % and 12 %
    const vals = [252, 99.9, 81, 41, 34, 28, 23, 20, 15, 12, 11, 9, 8, 7, 6, 5.5, 5, 4.5, 4, 3.6, 3.2, 3, 2.8, 2.5, 2.2, 2, 1.8, 1.5, 1.2, 1, 0.5, -0.4, -1.2, -4.7];
    const c = classify(vals, 'diverging');
    const colors = classColors(c, false);
    const neutral = vals.filter((x) => colorOf(c, colors, x) === '#f0efec').length;
    expect(neutral).toBeLessThan(vals.length / 3);
    expect(new Set(vals.map((x) => classOf(c, x))).size).toBeGreaterThanOrEqual(6);
    expect(c.breaks.slice(0, 4).map((x) => -x).reverse()).toEqual(c.breaks.slice(4)); // symmetric
  });

  it('diverging with few distinct magnitudes keeps colours aligned with classes', () => {
    const c = classify([-1, 1, 1, 1], 'diverging');
    expect(classColors(c, false)).toHaveLength(c.n);
    expect(classColors(c, false)[(c.n - 1) / 2]).toBe('#f0efec');
  });
});

describe('url state', () => {
  it('reads table + measure + filters, and the contract link v8Links.series', () => {
    const s = readState(new URLSearchParams('table=ilostat.une&measure=UNE&f.sex=SEX_F&period=2020&mode=bivarie&ytable=wb.x&ymeasure=Y&logx=1'));
    expect(s.x).toEqual({ table: 'ilostat.une', measure: 'UNE', filters: { sex: 'SEX_F' } });
    expect(s.y).toEqual({ table: 'wb.x', measure: 'Y', filters: {} });
    expect([s.period, s.mode, s.logx, s.logy, s.level, s.proj, s.scale]).toEqual(['2020', 'bivarie', true, false, 'adm0', 'exclude', 'auto']);
    const link = v8Links.series('wb.wdi_econ', { measure: 'NY.GDP.PCAP.CD' });
    expect(readState(new URL(link, 'http://x').searchParams).x).toEqual({ table: 'wb.wdi_econ', measure: 'NY.GDP.PCAP.CD', filters: {} });
    expect(readState(new URLSearchParams('table=wb.wdi_econ&indicator=NY.GDP.PCAP.CD')).x).toEqual({ series: 'wb.wdi_econ.NY.GDP.PCAP.CD' });
  });
  it('writes a selector, replacing the previous one', () => {
    const p = new URLSearchParams('table=a.b&measure=M&f.sex=F&period=2020');
    writeSelector(p, '', { table: 'c.d', measure: 'N', filters: { age: 'T' } });
    expect(p.toString()).toBe('period=2020&table=c.d&measure=N&f.age=T');
    expect(mondeHref({ series: 'wb.wdi_econ.X' })).toBe('/v8/monde?series=wb.wdi_econ.X');
    expect(selectorKey({ table: 'a.b', measure: 'M', filters: { b: '2', a: '1' } })).toBe('a.b|M|a=1|b=2');
  });
});

describe('feature', () => {
  it('declares the route, order and go key of the brief', () => {
    expect([feature.id, feature.path, feature.order, feature.goKey]).toEqual(['monde', 'monde', 20, 'm']);
  });
  it('palette search returns series hits linking to the map', async () => {
    api.getCatalog.mockResolvedValue({
      query: 'pib',
      concepts_total: 1,
      themes: {},
      hits: [{ kind: 'concept', id: 'wb.gdp_pc', label: 'PIB par habitant', sublabel: 'Current US$', table: 'wb.wdi_econ', measure: 'NY.GDP.PCAP.CD', filters: [], series: META.series, in_snapshot: true }],
    });
    const hits = await feature.search!('pib hab', new AbortController().signal);
    expect(hits[0]).toMatchObject({ group: 'Séries', href: '/v8/monde?table=wb.wdi_econ&measure=NY.GDP.PCAP.CD' });
  });
});

// ------------------------------------------------------------------ page

describe('MondePage', () => {
  it('shows an empty state that explains how to start (no fabricated map)', async () => {
    await renderPage('/v8/monde');
    expect(screen.getByText('Choisissez un indicateur')).toBeInTheDocument();
    expect(api.getMap).not.toHaveBeenCalled();
  });

  it('answers first, ranks the values, and hatches missing places with their reason (never 0)', async () => {
    await renderPage('/v8/monde?table=wb.wdi_econ&measure=NY.GDP.PCAP.CD');
    const answer = await screen.findByTestId('monde-answer');
    expect(within(answer).getByRole('heading').textContent).toContain('PIB par habitant');
    expect(within(answer).getByText(/Allemagne|Germany/)).toBeInTheDocument(); // maximum
    expect(api.getMap).toHaveBeenCalledWith({ table: 'wb.wdi_econ', measure: 'NY.GDP.PCAP.CD', filters: {} }, expect.objectContaining({ period: null }));
    // map fills: values coloured, gaps typed missing
    const map = await screen.findByTestId('monde-map');
    expect(within(map).getByText('ITA').getAttribute('data-state')).toBe('missing');
    expect(within(map).getByText('FRA').getAttribute('data-state')).toBe('value');
    expect(screen.getByTestId('tip-ITA').textContent).toContain('manquant : non rapporté par la source');
    expect(screen.getByTestId('tip-FRA').textContent).toContain('licence CC-BY-4.0');
    // legend lists the missing reasons with counts
    const legend = screen.getByTestId('monde-legend');
    expect(legend.textContent).toContain('jamais rapporté par cette série');
    expect(legend.textContent).not.toMatch(/\b0 Current/);
  });

  it('moves the period through the URL (slider) and opens the place panel on click', async () => {
    await renderPage('/v8/monde?table=wb.wdi_econ&measure=NY.GDP.PCAP.CD');
    const slider = await screen.findByRole('slider', { name: 'Période' });
    fireEvent.change(slider, { target: { value: '0' } });
    await waitFor(() => expect(screen.getByTestId('loc').textContent).toContain('period=2021'));
    fireEvent.click(within(await screen.findByTestId('monde-map')).getByText('FRA'));
    await waitFor(() => expect(screen.getByTestId('loc').textContent).toContain('sel=FRA'));
    const panel = await screen.findByTestId('place-panel');
    await waitFor(() => expect(within(panel).getByText(/dernière valeur observée · 2022/)).toBeInTheDocument());
    expect(within(panel).getByText(/à partir de 2023/)).toBeInTheDocument(); // projection named, not mixed
    expect(within(panel).getByRole('link', { name: /Fiche/ }).getAttribute('href')).toBe(v8Links.entity('country', 'FRA'));
    expect(within(panel).getByRole('link', { name: /Comparer/ }).getAttribute('href')).toBe(v8Links.compare(['wb.wdi_econ.NY.GDP.PCAP.CD'], ['FRA']));
    expect(screen.getByRole('button', { name: 'Épingler' })).toBeInTheDocument();
  });

  it('marks projection-only periods and offers the projection toggle', async () => {
    await renderPage('/v8/monde?table=wb.wdi_econ&measure=NY.GDP.PCAP.CD');
    const select = await screen.findByRole('combobox', { name: /Projections/ });
    act(() => {
      fireEvent.change(select, { target: { value: 'include' } });
    });
    await waitFor(() => expect(screen.getByTestId('loc').textContent).toContain('proj=include'));
  });
});
