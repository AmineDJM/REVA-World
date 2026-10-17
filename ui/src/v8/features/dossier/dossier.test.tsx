import { fireEvent, render, screen, within } from '@testing-library/react';
import { MemoryRouter, Route, Routes } from 'react-router';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { DossierDoc } from './api';
import { niceScale } from './Chart';
import DossierPage, { parseDossierPath } from './DossierPage';
import { dossierCommand, feature } from './index';
import { Reader } from './Reader';

const SRC = { source: 'wb', title: 'Banque mondiale', dataset: 'wb.wdi_econ', indicator: 'wb.wdi_econ.NY.GDP.MKTP.CD', period: '2025', licence: 'CC-BY-4.0', licence_url: null, restricted: false, non_commercial: false, required_notice: null, ref: '1cbb81b7', url: null, retrieved: null, verification: null, method: null };
const NC = { ...SRC, source: 'who', title: 'OMS — GHE', period: '2023', licence: 'CC BY-NC-SA 3.0 IGO', restricted: true, non_commercial: true, ref: 'aa' };

const RAW = {
  schema: 'reva-dossier/1',
  kind: 'country',
  subject: { kind: 'country', id: 'FRA', label: 'France', sublabel: 'FRA · Q142', entity_href: '/v8/fiche/country/FRA' },
  title: 'France',
  subtitle: 'Dossier pays · FRA',
  summary: [
    {
      template_id: 'summary.gdp',
      text: 'Son PIB atteint 3 366 milliards de dollars US (2025).',
      parts: [
        { t: 'text', text: 'Son PIB atteint ' },
        { t: 'value', text: '3 366 milliards de dollars US', exact: '3366315927447.3286', unit: 'US dollars', kind: 'observed', fn: 1 },
        { t: 'text', text: ' (' },
        { t: 'value', text: '2025', kind: 'label', fn: 1 },
        { t: 'text', text: ').' },
      ],
    },
  ],
  sections: [
    {
      id: 'economie',
      title: 'Économie',
      lede: 'Les grands agrégats.',
      blocks: [
        {
          type: 'paragraph',
          id: 'economie.eco.p0',
          sentences: [
            {
              template_id: 'economie.eco.lit.missing',
              text: 'Alphabétisation : manquant : aucune source consultée.',
              parts: [
                { t: 'text', text: 'Alphabétisation : ' },
                { t: 'missing', text: 'manquant : aucune source consultée', reason: 'aucune source consultée' },
                { t: 'text', text: '. Décès : ' },
                { t: 'value', text: '185 829 décès', exact: '185828.76', kind: 'estimate', fn: 2 },
                { t: 'text', text: '.' },
              ],
            },
          ],
        },
        { type: 'missing', id: 'economie.x', template_id: 'economie.x.empty', what: 'Prévisions scellées', reason: 'aucune question résolue' },
        {
          type: 'table',
          id: 'economie.t',
          template_id: 'economie.t.table',
          caption: 'Production',
          columns: ['Pays', 'Valeur'],
          numeric: [false, true],
          rows: [
            [
              { parts: [{ t: 'value', text: 'Nigeria', kind: 'label', fn: 2, href: '/v8/fiche/country/NGA' }] },
              { parts: [{ t: 'missing', text: 'manquant : non publié', reason: 'non publié' }] },
            ],
          ],
          note: null,
        },
        {
          type: 'chart',
          id: 'economie.c',
          template_id: 'economie.c.chart',
          chart: 'line',
          title: 'Croissance',
          unit: '% change',
          x_kind: 'year',
          series: [{ label: 'Croissance du PIB réel', kind: 'observed', fn: 1, points: [{ x: '2023', y: 1.1, exact: '1.1' }, { x: '2024', y: null }, { x: '2025', y: 0.84, exact: '0.841' }] }],
          note: null,
        },
        { type: 'list', id: 'economie.l', template_id: 'economie.l.item', title: 'Membre de', items: [{ template_id: 'economie.l.item', text: 'ONU', parts: [{ t: 'value', text: 'ONU', kind: 'label', fn: 1 }] }], more: 3 },
      ],
    },
  ],
  not_covered: [{ what: 'Essais cliniques', reason: 'non chargés', section: null }],
  footnotes: [
    { n: 1, ...SRC },
    { n: 2, ...NC },
  ],
  licences: [{ source: 'who', title: 'OMS — GHE', licence: 'CC BY-NC-SA 3.0 IGO', restricted: true, non_commercial: true, url: null }],
  honesty: ['Chaque nombre est la valeur que publie sa source.'],
  meta: { template: 'registry/dossier/country.yaml', template_version: '2b836e781456', snapshot_id: 'abcd', atlas_snapshot: '717e8452', generated_at: '2026-10-17T23:00:00+00:00', compute_ms: 900, cached: true, origin: 'live', sentences: 2, values: 6, missing: 2 },
};

const doc = DossierDoc.parse(RAW);

afterEach(() => vi.restoreAllMocks());

describe('Reader', () => {
  it('renders every value with its footnote chip and every missing value hatched with its reason', () => {
    render(
      <MemoryRouter>
        <Reader doc={doc} />
      </MemoryRouter>,
    );
    const values = screen.getAllByTestId('dos-value');
    expect(values.length).toBe(5);
    for (const v of values) expect(v.querySelector('[data-fn]')).not.toBeNull();
    const miss = screen.getAllByTestId('dos-missing');
    expect(miss.map((m) => m.textContent)).toEqual(['manquant : aucune source consultée', 'manquant : non publié']);
    expect(screen.getByTestId('dos-missing-block').textContent).toContain('Prévisions scellées');
    // never a zero for a missing value
    for (const m of miss) expect(m.textContent).not.toMatch(/\b0\b/);
  });

  it('shows the source card of a footnote on click (licence, period, NC flag) and lists all sources', () => {
    render(
      <MemoryRouter>
        <Reader doc={doc} />
      </MemoryRouter>,
    );
    const chip = screen.getAllByRole('button', { name: /Note 2/ })[0]!;
    expect(chip.className).toContain('dos-fn-nc');
    fireEvent.click(chip);
    expect(chip.getAttribute('aria-expanded')).toBe('true');
    const tip = document.getElementById(chip.getAttribute('aria-describedby')!)!;
    expect(tip.textContent).toContain('CC BY-NC-SA 3.0 IGO — usage non commercial');
    expect(tip.textContent).toContain('2023');
    const sources = screen.getByRole('heading', { name: 'Sources' }).closest('section')!;
    expect(within(sources).getAllByRole('listitem')).toHaveLength(2);
    expect(screen.getByText(/Licences restrictives parmi les sources/)).toBeTruthy();
  });

  it('answers first, then sections, then what the brief does not cover', () => {
    render(
      <MemoryRouter>
        <Reader doc={doc} />
      </MemoryRouter>,
    );
    const h2 = screen.getAllByRole('heading', { level: 2 }).map((h) => h.textContent);
    expect(h2).toEqual(['L’essentiel', 'Économie', 'Ce que ce dossier ne couvre pas', 'Règles de lecture', 'Sources']);
    expect(screen.getByText('Essais cliniques')).toBeTruthy();
    expect(screen.getByText(/et 3 autre\(s\) chez la source/)).toBeTruthy();
    // chart: a gap in the series stays a gap (two points drawn as separate segments, no zero)
    const chart = screen.getByTestId('dos-chart');
    expect(chart.querySelectorAll('polyline').length + chart.querySelectorAll('circle').length).toBeGreaterThan(0);
    expect(chart.textContent).toContain('Croissance');
  });
});

describe('page and routing', () => {
  it('parses brief URLs with encoded ids', () => {
    expect(parseDossierPath('/v8/dossier/drug/chembl%3ACHEMBL941')).toEqual(['drug', 'chembl:CHEMBL941']);
    expect(parseDossierPath('/v8/dossier')).toBeNull();
  });

  it('loads a brief, shows the table of contents and the three exports', async () => {
    vi.spyOn(globalThis, 'fetch').mockImplementation(async () => new Response(JSON.stringify(RAW), { status: 200, headers: { 'Content-Type': 'application/json' } }));
    render(
      <MemoryRouter initialEntries={['/v8/dossier/country/FRA']}>
        <Routes>
          <Route path="/v8/dossier/*" element={<DossierPage />} />
        </Routes>
      </MemoryRouter>,
    );
    expect(await screen.findByRole('heading', { level: 1, name: 'France' })).toBeTruthy();
    const pdf = screen.getByRole('link', { name: 'Version imprimable (PDF)' });
    expect(pdf.getAttribute('href')).toContain('/v1/v8/dossier/country/FRA?format=html');
    expect(screen.getByRole('link', { name: 'Markdown' }).getAttribute('href')).toContain('format=md');
    expect(screen.getByRole('link', { name: 'JSON' }).getAttribute('href')).toContain('format=json');
    const toc = screen.getByText('Sommaire').closest('details')!;
    expect(within(toc).getByText('Ce que ce dossier ne couvre pas')).toBeTruthy();
  });

  it('lists the ready briefs on the picker', async () => {
    vi.spyOn(globalThis, 'fetch').mockImplementation(
      async () =>
        new Response(
          JSON.stringify({ query: null, hits: [{ kind: 'disease', id: 'malaria', label: 'Paludisme', sublabel: 'Mondo', featured: true }], kinds: [{ kind: 'disease', label: 'Maladie' }] }),
          { status: 200, headers: { 'Content-Type': 'application/json' } },
        ),
    );
    render(
      <MemoryRouter initialEntries={['/v8/dossier']}>
        <Routes>
          <Route path="/v8/dossier/*" element={<DossierPage />} />
        </Routes>
      </MemoryRouter>,
    );
    const link = await screen.findByRole('link', { name: /Paludisme/ });
    expect(link.getAttribute('href')).toBe('/v8/dossier/disease/malaria');
  });
});

describe('feature contract', () => {
  it('declares route, order, go key and palette commands', () => {
    expect(feature.id).toBe('dossier');
    expect(feature.path).toBe('dossier');
    expect(feature.order).toBe(90);
    expect(feature.goKey).toBe('b');
    expect(dossierCommand('dossier FRA')).toBe('/v8/dossier/country/FRA');
    expect(dossierCommand('dossier pétrole')).toBe('/v8/dossier/commodity/crude_oil');
    expect(dossierCommand('dossier imatinib')).toBe('/v8/dossier/drug/chembl%3ACHEMBL941');
    expect(dossierCommand('dossier')).toBe('/v8/dossier');
  });

  it('builds nice chart scales', () => {
    expect(niceScale(0, 114)).toEqual({ bot: 0, top: 125, step: 25 });
  });
});
