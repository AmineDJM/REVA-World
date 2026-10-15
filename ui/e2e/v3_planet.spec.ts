/**
 * V3 "Planète" screens end to end (Neurones, Chemin, Frise, Atlas, Dossier pays, Demander, palette).
 *
 * The V3 APIs (/v1/kg, /v1/atlas, /v1/links, /v1/ask) are answered by Playwright route interception
 * with the fixtures in src/v3/test/fixtures.ts (shapes of the real routes, labelled test_fixture),
 * because the e2e stack does not load V3 data yet (see the UI3 report, cross-owner request for an
 * `e2e_stack.py --v3-fixtures` mode). Display geometry (/v1/locations) is a fixture too.
 *
 * Two ways to run:
 *  - against the e2e stack (real session + built UI):   uv run python scripts/e2e_stack.py --run -- e2e/v3_planet.spec.ts
 *  - standalone (no database): build the UI, serve it with `npx vite preview --port 4173`, then
 *      REVA_E2E_V3_URL=http://127.0.0.1:4173 PLAYWRIGHT_BROWSERS_PATH=/opt/pw-browsers npx playwright test e2e/v3_planet.spec.ts
 *    (the session endpoint is intercepted too, so no login is needed).
 */
import { expect, test, type Page, type Route } from '@playwright/test';
import { BASE_URL, HAVE_SESSION, STORAGE_STATE } from './helpers';
import * as F from '../src/v3/test/fixtures';

const STANDALONE_URL = process.env.REVA_E2E_V3_URL ?? '';
const STANDALONE = !HAVE_SESSION && !!STANDALONE_URL;
const BASE = HAVE_SESSION ? BASE_URL : STANDALONE_URL;

test.skip(!BASE, 'No REVA e2e stack and no REVA_E2E_V3_URL (vite preview of the built UI)');
test.use({ baseURL: BASE, ...(HAVE_SESSION ? { storageState: STORAGE_STATE } : {}) });

const json = (route: Route, body: unknown, status = 200) =>
  route.fulfill({ status, contentType: status >= 400 ? 'application/problem+json' : 'application/json', body: JSON.stringify(body) });
const notFound = (route: Route, detail = 'entity not found in the knowledge graph') => json(route, { title: 'Not Found', status: 404, detail, code: 'not_found' }, 404);

/** Route V3 endpoints (and, standalone, the session) to fixtures. */
async function installFixtures(page: Page) {
  await page.route(/\/v1\//, async (route) => {
    const req = route.request();
    const url = new URL(req.url());
    const p = decodeURIComponent(url.pathname);
    const q = url.searchParams;
    let m: RegExpMatchArray | null;
    if (p === '/v1/session' && req.method() === 'GET') {
      if (STANDALONE) return json(route, { username: 'e2e', principal: 'user:e2e', auth_method: 'session', scopes: ['read'] });
      return route.fallback();
    }
    if ((m = p.match(/^\/v1\/kg\/entities\/([^/]+)\/neighbours$/))) {
      if (m[1] === 'Q155') return json(route, F.NEIGHBOURS_Q155);
      if (m[1] === 'Q5146') return json(route, F.NEIGHBOURS_Q5146);
      if (F.ENTITIES[m[1]!]) return json(route, F.envelope({ qid: m[1], label: F.ENTITIES[m[1]!]!.labels.en, edges: [] }));
      return notFound(route);
    }
    if ((m = p.match(/^\/v1\/kg\/entities\/([^/]+)\/timeline$/))) {
      if (m[1] === 'Q155') return json(route, F.TIMELINE_Q155);
      if (m[1] === 'Q142') return json(route, F.TIMELINE_Q142);
      return notFound(route);
    }
    if ((m = p.match(/^\/v1\/kg\/entities\/([^/]+)$/))) {
      const e = F.ENTITIES[m[1]!];
      return e ? json(route, F.envelope(e)) : notFound(route);
    }
    if (p === '/v1/kg/search') return json(route, F.SEARCH_BRESIL);
    if (p === '/v1/kg/path') return json(route, F.KG_PATH_COL_BRA);
    if (p === '/v1/links/path') return json(route, F.LINKS_PATH_COL_BRA);
    if ((m = p.match(/^\/v1\/links\/([^/]+)$/))) return m[1] === 'FRA' ? json(route, F.LINKS_FRA) : notFound(route, 'country not found');
    if (p === '/v1/atlas/concepts') return json(route, F.CONCEPTS);
    if (p === '/v1/atlas/coverage') return json(route, q.get('concept') ? F.COVERAGE_UNEMP : F.COVERAGE_ANY);
    if (p === '/v1/atlas/map') return json(route, F.mapResponse(Number(q.get('year'))));
    if (p === '/v1/atlas/countries/FRA') return json(route, F.DOSSIER_FRA);
    if (p.startsWith('/v1/atlas/countries/')) return json(route, { title: 'Not Found', status: 404, detail: 'no atlas snapshot recorded', code: 'atlas_snapshot_unavailable' }, 404);
    if (p === '/v1/locations') return json(route, F.LOCATIONS);
    if (p === '/v1/ask' && req.method() === 'POST') return json(route, F.ASK_BRESIL);
    if (STANDALONE) return json(route, F.NOT_FOUND_ROUTE, 404);
    return route.fallback();
  });
}

test.beforeEach(async ({ page }) => {
  await installFixtures(page);
});

async function noLoading(page: Page) {
  await expect(page.locator('.state-loading')).toHaveCount(0, { timeout: 30_000 });
}

test('Neurones: graph canvas, entity card with provenance, statements with Wikidata citations, expansion and "as of" year', async ({ page }) => {
  await page.goto('/v3/neurones?qid=Q155');
  await expect(page.getByRole('heading', { level: 1, name: /Neurones/ })).toBeVisible();
  const card = page.getByTestId('entity-card');
  await expect(card.getByRole('heading', { name: /Brésil/ })).toBeVisible();
  await expect(card.getByTestId('community-badge')).toBeVisible();
  await expect(page.getByTestId('neuron-canvas').locator('canvas').first()).toBeAttached();
  await expect(page.getByTestId('neuron-canvas')).toHaveAttribute('aria-label', /5 entités et 4 déclarations/);
  const cites = page.getByTestId('citation');
  await expect(cites).toHaveCount(4);
  await expect(cites.first().getByRole('link').first()).toHaveAttribute('href', /^https:\/\/www\.wikidata\.org\/wiki\/Q\d+#Q\d+\$FIXTURE-/);

  // Keyboard-accessible node list: open "portugais" -> its card, and its neighbours are fetched.
  const expanded = page.waitForRequest((r) => r.url().includes('/v1/kg/entities/Q5146/neighbours'));
  await page.getByText(/Liste des nœuds/).click();
  await page.getByRole('button', { name: /Portuguese|portugais/ }).first().click();
  await expanded;
  await expect(page.getByTestId('entity-card').getByRole('heading', { name: /portugais/ })).toBeVisible();

  // "As of" 1900: UN membership (1945) is not in force.
  await page.getByLabel('filtrer par année').click();
  await expect(page.getByLabel('filtrer par année')).toBeChecked();
  await page.getByRole('slider', { name: 'Année' }).fill('1900');
  await expect(page.locator('.v3-stage-stats')).toContainText('en vigueur en 1900');
});

test('Chemin: banner, k animated chains, "relié via" hops with citations, causal wording only where the source asserts it', async ({ page }) => {
  await page.goto('/v3/chemin?from=Q7322&to=Q155');
  await expect(page.getByTestId('path-not-cause')).toContainText('Un chemin n’est pas une cause.');
  const chains = page.getByTestId('path-chain');
  await expect(chains).toHaveCount(2);
  await expect(chains.nth(0).getByTestId('path-hop').nth(0)).toContainText('relié via « participant à »');
  await expect(chains.nth(0).getByTestId('causal-wording')).toHaveCount(0);
  await expect(page.getByTestId('causal-wording')).toHaveCount(1);
  await expect(page.getByTestId('causal-wording')).toContainText('non établie par REVA');
  for (const hop of await page.getByTestId('path-hop').all()) expect(await hop.getByTestId('citation').count()).toBeGreaterThan(0);
});

test('Frise: SVG timeline with zoom from centuries to years and cited items', async ({ page }) => {
  await page.goto('/v3/frise?qid=Q155');
  const svg = page.getByTestId('timeline-svg');
  await expect(svg).toBeVisible();
  const controls = page.getByRole('toolbar', { name: 'Zoom de la frise' });
  const before = await controls.locator('.muted').first().textContent();
  await controls.getByRole('button', { name: 'Zoomer', exact: true }).click();
  await controls.getByRole('button', { name: 'Zoomer', exact: true }).click();
  await expect(controls.locator('.muted').first()).not.toHaveText(before ?? '');
  await page.getByRole('heading', { name: /Éléments datés/ }).scrollIntoViewIfNeeded();
  await expect(page.locator('.v3-tl-row').first()).toBeVisible();
  await expect(page.locator('.v3-tl-row [data-testid="citation"]').first()).toBeVisible();
});

test('Atlas: choropleth, hatched gaps with reason (never zero), legend with sources, ranking, year play', async ({ page }) => {
  await page.goto('/v3/atlas?concept=labour.unemployment_rate');
  await expect(page.getByTestId('atlas-legend')).toContainText('manquant — 3 pays');
  await expect(page.getByTestId('atlas-map').locator('canvas')).toBeAttached();
  const ranking = page.getByTestId('atlas-ranking');
  await expect(ranking.getByRole('row').first()).toHaveAttribute('data-iso3', 'ZAF');
  await page.getByText(/Pays sans valeur en 2022/).click();
  const gap = page.locator('[data-gap-country="SOM"]');
  await expect(gap).toContainText('manquant (aucune source déclarée');
  expect(await gap.textContent()).not.toMatch(/\b0\b/);

  // Play from 2020: the year advances (one map request per year).
  await page.goto('/v3/atlas?concept=labour.unemployment_rate&annee=2020');
  await expect(page.locator('.v3-year-out')).toHaveText('2020');
  await page.getByRole('button', { name: /Lecture/ }).click();
  await expect(page.locator('.v3-year-out')).toHaveText('2021', { timeout: 10_000 });
});

test('Atlas map tooltip names the gap reason for a hatched country', async ({ page }) => {
  await page.goto('/v3/atlas?concept=labour.unemployment_rate');
  const map = page.getByTestId('atlas-map');
  await expect(map.locator('canvas')).toBeAttached();
  await page.waitForTimeout(800);
  const box = (await map.boundingBox())!;
  const tip = page.getByTestId('atlas-map-tip');
  let found = '';
  // Somalia's fixture rectangle (41..51 E, -2..12 N) lies right of and slightly below the centre (10 E, 20 N).
  outer: for (let dy = 0; dy <= 120; dy += 8) {
    for (let dx = 40; dx <= 180; dx += 8) {
      await page.mouse.move(box.x + box.width / 2 + dx, box.y + box.height / 2 + dy);
      if (await tip.count()) {
        const t = (await tip.textContent()) ?? '';
        if (t.includes('Somalia')) {
          found = t;
          break outer;
        }
      }
    }
  }
  expect(found).toContain('donnée manquante');
  expect(found).not.toMatch(/\b0\b/);
});

test('Dossier pays: values with source and year, missing cells with sources searched, links and history', async ({ page }) => {
  await page.goto('/v3/pays/FRA');
  await expect(page.getByRole('heading', { level: 1, name: /France/ })).toBeVisible();
  const row = page.locator('[data-concept="labour.unemployment_rate"]');
  await expect(row).toContainText('7,3 %');
  await expect(row).toContainText('2022');
  await page.getByText(/Sans donnée pour ce pays/).first().click();
  await expect(page.getByTestId('missing-labour')).toContainText('sources consultées : ilo.ilostat_mw.EAR_INEE_NOC_NB');
  await expect(page.getByRole('link', { name: 'Espagne' })).toBeVisible();
  await expect(page.getByText('Ouvrir la frise complète')).toBeVisible();
});

test('Demander: cited sentences, evidence and the collapsible plan', async ({ page }) => {
  await page.goto('/v3/demander');
  await expect(page.getByTestId('sourced-only')).toBeVisible();
  await page.getByLabel('Votre question').fill('Quelle est la langue officielle du Brésil ?');
  await page.getByRole('button', { name: 'Demander' }).click();
  const sentences = page.getByTestId('answer-sentence');
  await expect(sentences).toHaveCount(3);
  await sentences.nth(0).getByRole('link', { name: 'E1' }).click();
  await expect(page.locator('#ev-E1')).toHaveClass(/active/);
  await page.getByText(/Plan de requête/).click();
  await expect(page.locator('.v3-plan pre')).toContainText('kg_statements');
});

test('an endpoint that is not deployed yet shows "pas encore disponible", never fake data', async ({ page }) => {
  await page.route(/\/v1\/kg\//, (route) => json(route, F.NOT_FOUND_ROUTE, 404));
  await page.goto('/v3/neurones?qid=Q155');
  await expect(page.getByText('Pas encore disponible').first()).toBeVisible();
  await expect(page.getByTestId('neuron-canvas')).toHaveCount(0);
});

test('command palette opens every V3 screen', async ({ page }) => {
  await page.goto('/v3');
  await expect(page.getByRole('heading', { name: /La planète, reliée/ })).toBeVisible();
  await page.keyboard.press('Control+k');
  const input = page.getByRole('combobox', { name: /Search/ });
  await input.fill('Chemin');
  await page.keyboard.press('Enter');
  await expect(page).toHaveURL(/\/v3\/chemin/);
  for (const [q, url] of [
    ['Neurones', /\/v3\/neurones/],
    ['Frise', /\/v3\/frise/],
    ['Atlas — carte', /\/v3\/atlas/],
    ['Dossier pays — tous', /\/v3\/pays\/FRA/],
    ['Demander', /\/v3\/demander/],
  ] as const) {
    await page.keyboard.press('Control+k');
    await page.getByRole('combobox', { name: /Search/ }).fill(q);
    await page.keyboard.press('Enter');
    await expect(page).toHaveURL(url);
  }
});

test('375 px: no horizontal page scroll on V3 screens', async ({ page }) => {
  await page.setViewportSize({ width: 375, height: 800 });
  for (const path of ['/v3', '/v3/neurones?qid=Q155', '/v3/chemin?from=Q7322&to=Q155', '/v3/frise?qid=Q155', '/v3/atlas?concept=labour.unemployment_rate', '/v3/pays/FRA', '/v3/demander']) {
    await page.goto(path);
    await noLoading(page);
    const overflow = await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
    expect(overflow, `${path} scrolls horizontally by ${overflow}px`).toBeLessThanOrEqual(1);
  }
});
