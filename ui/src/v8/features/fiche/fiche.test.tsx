/**
 * Fiche (V8FICHE) against responses recorded from the dev API (GET /v1/v8/entity/..., 2026-10-18; lists trimmed,
 * values unchanged): France, crude oil (anchor page), imatinib (drug, salt form), a UCDP conflict.
 */
import { configure, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { MemoryRouter, Route, Routes, useLocation } from 'react-router';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { mockFetch } from '../../../test/fetchMock';
import CONFLICT from './__fixtures__/conflict.json';
import COMMODITY from './__fixtures__/commodity.json';
import COUNTRY from './__fixtures__/country.json';
import DRUG from './__fixtures__/drug.json';
import { Bundle } from './api';
import FichePage from './FichePage';
import { ficheUrlFor, feature } from './index';
import { bigFr, citation, linePoints, sections, uniqueIds, unitFr } from './model';

configure({ asyncUtilTimeout: 5000 });
vi.setConfig({ testTimeout: 20_000 });
afterEach(() => vi.unstubAllGlobals());

function Where() {
  const l = useLocation();
  return <span data-testid="where">{l.pathname}</span>;
}

function renderAt(path: string) {
  return render(
    <MemoryRouter initialEntries={[path]}>
      <Routes>
        <Route path="/v8/fiche/:kind/:id" element={<FichePage />} />
      </Routes>
      <Where />
    </MemoryRouter>,
  );
}

describe('feature contract', () => {
  it('is a hidden route with params, reachable by commands', () => {
    expect(feature.id).toBe('fiche');
    expect(feature.path).toBe('fiche/:kind/:id');
    expect(feature.navHidden).toBe(true);
    expect(feature.navPath).toBeUndefined(); // a static nav route would shadow the param route
    expect(ficheUrlFor('fiche FRA')).toBe('/v8/fiche/country/FRA');
    expect(ficheUrlFor('fra')).toBe('/v8/fiche/country/FRA');
    expect(ficheUrlFor('CHEBI:27732')).toBe('/v8/fiche/entity/chebi%3A27732');
    expect(ficheUrlFor('Q142')).toBe('/v8/fiche/entity/Q142');
    expect(ficheUrlFor('pétrole brut')).toBe('/v8/fiche/commodity/crude_oil');
    expect(ficheUrlFor('hello world')).toBeNull();
  });
});

describe('recorded bundles parse', () => {
  it.each([
    ['country', COUNTRY],
    ['commodity', COMMODITY],
    ['drug', DRUG],
    ['conflict', CONFLICT],
  ])('%s', (_n, fx) => {
    const b = Bundle.parse((fx as { data: unknown }).data);
    expect(b.headline.length).toBeGreaterThanOrEqual(3);
    expect(b.headline.length).toBeLessThanOrEqual(6);
    // honesty: a stat without value has a typed reason, a number has its source
    for (const s of b.headline) {
      if (s.value === null && s.value_text === null && s.ref === null) expect(s.missing?.reason_fr).toBeTruthy();
      if (s.value !== null && s.kind !== 'count') expect(s.source).not.toBeNull();
    }
    expect(sections(b).map((x) => x.id)).toEqual(['faits', 'relations', 'series', 'carte', 'causes', 'previsions', 'manques', 'sources']);
  });

  it('refuses a number where a decimal string is required', () => {
    const b = structuredClone((COUNTRY as { data: { headline: { value: unknown }[] } }).data);
    b.headline[0]!.value = 66438822;
    expect(Bundle.safeParse(b).success).toBe(false);
  });
});

describe('FichePage', () => {
  it('country/FRA: answer first (name, ids, six headline numbers with sources), all sections', async () => {
    const { calls } = mockFetch({ 'GET /v1/v8/entity/country/FRA': { body: COUNTRY } });
    renderAt('/v8/fiche/country/FRA');
    expect(await screen.findByRole('heading', { level: 1, name: 'France' })).toBeInTheDocument();
    expect(calls).toHaveLength(1); // ONE bundle call per page
    const head = screen.getByTestId('fiche-headline');
    for (const l of ['Population', 'PIB (US$ courants)', 'Inflation (IPC)', 'Espérance de vie', 'Chômage']) expect(within(head).getByText(l)).toBeInTheDocument();
    expect(within(head).getAllByTestId('v8-source').length).toBeGreaterThanOrEqual(6);
    for (const t of ['Faits', 'Relations', 'Séries', 'Carte & temps', 'Causes & effets', 'Prévisions', 'Ce qui manque', 'Sources']) expect(screen.getByRole('heading', { level: 2, name: t })).toBeInTheDocument();
    expect(screen.getByTestId('fiche-map')).toBeInTheDocument();
    // ISO alpha-3 listed once although two sources give it
    expect(screen.getAllByText((_, el) => el?.className === 'fiche-id' && /ISO 3166-1 alpha-3\s*FRA/i.test(el.textContent ?? '')).length).toBe(1);
    // series open Monde through v8Links.series
    const monde = screen.getAllByRole('link', { name: /Monde/ })[0]!;
    expect(monde.getAttribute('href')).toMatch(/^\/v8\/monde\?table=/);
    // pending forecasts are not judged
    for (const q of screen.getAllByTestId('fiche-question')) expect(q.textContent).toMatch(/pas encore jugée|résolue|annulée/);
    // Missing is hatched with a reason, never 0
    const miss = screen.getByTestId('fiche-missing');
    expect(within(miss).getAllByTestId('v8-missing').length).toBeGreaterThan(0);
    expect(document.title).toBe('France · Fiche');
  });

  it('relations link to their Fiche; "Chemin vers…" opens Liens', async () => {
    mockFetch({ 'GET /v1/v8/entity/country/FRA': { body: COUNTRY } });
    renderAt('/v8/fiche/country/FRA');
    await screen.findByRole('heading', { level: 1, name: 'France' });
    const rel = document.getElementById('fiche-relations')!;
    const links = within(rel)
      .getAllByRole('link')
      .filter((a) => a.classList.contains('fiche-ent'));
    expect(links.length).toBeGreaterThan(3);
    for (const a of links) expect(a.getAttribute('href')).toMatch(/^\/v8\/fiche\/[^/]+\/[^/]+$/);
    expect(screen.getByRole('link', { name: 'Chemin vers…' }).getAttribute('href')).toBe('/v8/liens?from=Q142&to=');
  });

  it('a non-canonical URL is replaced by the canonical one (drug/CHEMBL941 → drug/chembl:CHEMBL941)', async () => {
    mockFetch({ 'GET /v1/v8/entity/drug/': { body: DRUG } });
    renderAt('/v8/fiche/drug/CHEMBL941');
    await screen.findByRole('heading', { level: 1, name: 'IMATINIB' });
    await waitFor(() => expect(screen.getByTestId('where').textContent).toBe('/v8/fiche/drug/chembl%3ACHEMBL941'));
    // the salt form carries the targets: shown "via" it
    expect(screen.getAllByText(/IMATINIB MESYLATE/).length).toBeGreaterThan(0);
    expect(screen.getByText('Cibles (mécanisme)')).toBeInTheDocument();
  });

  it('anchor page is labelled as a REVA grouping; out-of-snapshot values are flagged', async () => {
    mockFetch({ 'GET /v1/v8/entity/commodity/crude_oil': { body: COMMODITY } });
    renderAt('/v8/fiche/commodity/crude_oil');
    await screen.findByRole('heading', { level: 1, name: 'Pétrole brut' });
    expect(screen.getAllByText('page d’ancrage REVA').length).toBeGreaterThan(0);
    expect(screen.getAllByTestId('fiche-top').length).toBeGreaterThan(0);
    const b = Bundle.parse((COMMODITY as { data: unknown }).data);
    if (b.series.some((s) => s.out_of_snapshot)) expect(screen.getAllByText('hors instantané').length).toBeGreaterThan(0);
  });

  it('conflict: dated deaths become a series; causal claims are attributed with tier and grade', async () => {
    mockFetch({ 'GET /v1/v8/entity/armed_conflict/': { body: CONFLICT } });
    renderAt('/v8/fiche/armed_conflict/ucdp%3Aconflict%3A13243');
    await screen.findByRole('heading', { level: 1 });
    const b = Bundle.parse((CONFLICT as { data: unknown }).data);
    expect(b.series.some((s) => s.key === 'ucdp:battle_related_deaths' && s.points.length >= 2)).toBe(true);
    if (b.claims.n_in + b.claims.n_out > 0) {
      const causes = document.getElementById('fiche-causes')!;
      expect(within(causes).getAllByText(/^note [A-E]$|non noté/).length).toBeGreaterThan(0);
    }
  });

  it('keyboard: j/k move between sections, c copies the citation, p pins (lazy Tableaux API)', async () => {
    mockFetch({ 'GET /v1/v8/entity/country/FRA': { body: COUNTRY } });
    const writeText = vi.fn(async () => undefined);
    Object.defineProperty(navigator, 'clipboard', { value: { writeText }, configurable: true });
    renderAt('/v8/fiche/country/FRA');
    await screen.findByRole('heading', { level: 1, name: 'France' });
    const tab = () => document.querySelector('.fiche-tab.is-active')?.textContent ?? '';
    expect(tab()).toMatch(/^Faits/);
    fireEvent.keyDown(window, { key: 'j' });
    expect(tab()).toMatch(/^Relations/);
    fireEvent.keyDown(window, { key: 'j' });
    fireEvent.keyDown(window, { key: 'k' });
    expect(tab()).toMatch(/^Relations/);
    fireEvent.keyDown(window, { key: 'c' });
    await waitFor(() => expect(writeText).toHaveBeenCalledTimes(1));
    expect(String((writeText.mock.calls[0] as unknown[])[0])).toMatch(/^« France » \(Pays/);
    expect(await screen.findByText('Citation copiée.')).toBeInTheDocument();
    // "g" then "j" is the shell's go-to, not ours
    fireEvent.keyDown(window, { key: 'g' });
    fireEvent.keyDown(window, { key: 'j' });
    expect(tab()).toMatch(/^Relations/);
    // p pins through the Tableaux pin API (no /v1/v8/boards here: the board lives in this browser)
    await new Promise((r) => setTimeout(r, 1600));
    fireEvent.keyDown(window, { key: 'p' });
    expect(await screen.findByText(/Épinglé sur « Mes épingles »|Déjà sur/)).toBeInTheDocument();
  });

  it('an unknown id shows what was asked and what a Fiche accepts', async () => {
    mockFetch({ 'GET /v1/v8/entity/drug/': { status: 404, body: { type: 'about:blank', title: 'not found', status: 404, detail: 'no fiche for drug/NOPE: no KG record for this id', code: 'not_found' } } });
    renderAt('/v8/fiche/drug/NOPE');
    expect(await screen.findByText(/Une fiche accepte un code ISO3/)).toBeInTheDocument();
  });
});

describe('model helpers', () => {
  it('formats and dedupes', () => {
    expect(bigFr('3366315927447.33')).toBe('3,37 T');
    expect(bigFr('84383.37').replace(/\s/g, ' ')).toBe('84 383');
    expect(unitFr('persons')).toBe('personnes');
    expect(unitFr('Da')).toBe('Da');
    expect(uniqueIds([{ scheme: 'ISO', value: 'FRA' }, { scheme: 'iso', value: 'FRA' }])).toHaveLength(1);
    expect(linePoints([{ t: '2020', x: 2020, v: null, projection: false }])[0]!.y).toBeNull(); // gap, never 0
    const b = Bundle.parse((DRUG as { data: unknown }).data);
    expect(citation(b, 'https://x/v8/fiche/drug/chembl:CHEMBL941', '2026-10-18')).toMatch(/Sources : ChEMBL \(CC BY-SA 3\.0\)/);
  });
});
