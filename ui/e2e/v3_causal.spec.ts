/**
 * V3 "Causes & conséquences" end to end, intercepted mode: /v1/causal/* (and /v1/ask for the causal answers)
 * are answered by Playwright route interception with the fixtures in src/v3/causal/fixtures.ts (shapes of
 * CAUSCORE's contract, docs/dev/v3_causal_interface.md, labelled test_fixture) and
 * src/v3/causal/ask_causal.fixture.json (the real NLQA pipeline's output over test fixtures).
 *
 * Two ways to run (as e2e/v3_planet.spec.ts):
 *  - against the e2e stack (real session + built UI):   uv run python scripts/e2e_stack.py --run -- e2e/v3_causal.spec.ts
 *  - standalone (no database): build the UI, serve it with `npx vite preview --port 4173`, then
 *      REVA_E2E_V3_URL=http://127.0.0.1:4173 PLAYWRIGHT_BROWSERS_PATH=/opt/pw-browsers npx playwright test e2e/v3_causal.spec.ts
 */
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { expect, test, type Page, type Route } from '@playwright/test';
import { BASE_URL, HAVE_SESSION, STORAGE_STATE } from './helpers';
import * as F from '../src/v3/causal/fixtures';

const ASK = JSON.parse(readFileSync(fileURLToPath(new URL('../src/v3/causal/ask_causal.fixture.json', import.meta.url)), 'utf-8')) as Record<string, unknown>;

const STANDALONE_URL = process.env.REVA_E2E_V3_URL ?? '';
const STANDALONE = !HAVE_SESSION && !!STANDALONE_URL;
const BASE = HAVE_SESSION ? BASE_URL : STANDALONE_URL;

test.skip(!BASE, 'No REVA e2e stack and no REVA_E2E_V3_URL (vite preview of the built UI)');
test.use({ baseURL: BASE, ...(HAVE_SESSION ? { storageState: STORAGE_STATE } : {}) });

const json = (route: Route, body: unknown, status = 200) =>
  route.fulfill({ status, contentType: status >= 400 ? 'application/problem+json' : 'application/json', body: JSON.stringify(body) });

async function installFixtures(page: Page, calls: string[] = []) {
  await page.route(/\/v1\//, async (route) => {
    const req = route.request();
    const url = new URL(req.url());
    const p = decodeURIComponent(url.pathname);
    calls.push(`${req.method()} ${p}${url.search}`);
    if (p === '/v1/session' && req.method() === 'GET') {
      if (STANDALONE) return json(route, { username: 'e2e', principal: 'user:e2e', auth_method: 'session', scopes: ['read'] });
      return route.fallback();
    }
    if (p === '/v1/causal/events/Q361') return json(route, F.EVENT_WW1);
    if (p === '/v1/causal/events/Q361/causes') return json(route, F.CAUSES_WW1);
    if (p === '/v1/causal/events/Q361/effects') return json(route, F.EFFECTS_WW1);
    if (p === '/v1/causal/events/Q361/why') return json(route, F.WHY_WW1);
    if (p.startsWith('/v1/causal/events/')) return json(route, F.UNKNOWN_EVENT, 404);
    if (p === '/v1/causal/chain') {
      return url.searchParams.get('from') === 'fixture:sarajevo_1914' && url.searchParams.get('to') === 'fixture:versailles'
        ? json(route, F.CHAIN_SARAJEVO_VERSAILLES)
        : json(route, F.CHAIN_NONE);
    }
    if (p === '/v1/causal/candidates') return json(route, F.CANDIDATES_WW1);
    if (p === '/v1/kg/search') return json(route, { data: { query: url.searchParams.get('q') ?? '', results: [] }, warnings: [] });
    if (p === '/v1/ask' && req.method() === 'POST') {
      const q = String((req.postDataJSON() as { question?: string }).question ?? '');
      return json(route, /cha[iî]ne/i.test(q) ? ASK.chain : ASK.why);
    }
    if (STANDALONE) return json(route, F.NOT_FOUND_ROUTE, 404);
    return route.fallback();
  });
}

async function noLoading(page: Page) {
  await expect(page.locator('.state-loading')).toHaveCount(0, { timeout: 30_000 });
}

test.beforeEach(async ({ page }) => {
  await page.emulateMedia({ reducedMotion: 'reduce' });
});

test('graded causal graph: event card, legend, edges on the canvas; a clicked edge opens its sourced claim', async ({ page }) => {
  await installFixtures(page);
  await page.goto('/v3/causes?event=Q361');
  await expect(page.getByRole('heading', { level: 1, name: 'Causes & conséquences' })).toBeVisible();
  const card = page.getByTestId('event-card');
  await expect(card.getByRole('heading', { name: 'Première Guerre mondiale' })).toBeVisible();
  await expect(card.getByTestId('event-dates')).toContainText('28 juil. 1914 – 11 nov. 1918');
  await expect(page.getByTestId('causal-legend')).toContainText('Grade de preuve');
  const canvas = page.getByTestId('causal-canvas');
  await expect(canvas.locator('canvas').first()).toBeAttached();
  await expect(canvas).toHaveAttribute('data-edges', '10');

  // click the coded-trigger edge (ICB, T1) at its midpoint on the canvas
  const pt = await canvas.evaluate((el) => {
    const cy = (el as HTMLDivElement & { __cy?: { $id: (id: string) => { renderedMidpoint: () => { x: number; y: number } } } }).__cy!;
    return cy.$id('ds:icb.data:icb2:2:trigger').renderedMidpoint();
  });
  const box = (await canvas.boundingBox())!;
  await page.mouse.click(box.x + pt.x, box.y + pt.y);
  const detail = page.getByTestId('claim-detail');
  await expect(detail.getByTestId('claim-attribution')).toContainText('Affirmé selon icb.data (T1)');
  await expect(detail.getByTestId('claim-statement')).toContainText('révision v16-FIXTURE');
  await expect(detail.getByTestId('claim-mechanism')).toContainText('cité tel quel depuis la source');
  await expect(detail.getByTestId('temporal-badge')).toHaveAttribute('data-temporal', 'consistent');
  await expect(detail.getByTestId('claim-corroboration')).toContainText('2 groupes de sources indépendants');

  // the keyboard path: the claim lists open the same detail
  await page.getByTestId('causes-list').getByRole('button', { name: /Rivalités impériales.*selon Wikidata \(T3\)/ }).click();
  await expect(detail.getByTestId('claim-contested')).toContainText('lien nié');
});

test('"Pourquoi ?", dissent side by side and the timeline alignment', async ({ page }) => {
  await installFixtures(page);
  await page.goto('/v3/causes?event=Q361');
  const why = page.getByTestId('why-panel');
  await expect(why.getByTestId('why-immediate')).toContainText('selon Wikidata (T3)');
  await expect(why.getByTestId('why-underlying')).toContainText('selon fixture.scholarly (T2)');
  await expect(why.getByTestId('why-coded')).toContainText('selon icb.data (T1)');
  const pair = page.getByTestId('dissent-pair');
  await expect(pair).toHaveCount(1);
  await expect(pair.getByTestId('claim-line')).toHaveCount(2);
  const tl = page.getByTestId('causal-timeline');
  await expect(tl.locator('svg')).toBeVisible();
  await expect(tl.getByTestId('timeline-focal')).toBeAttached();
  await expect(tl.locator('[data-lane="effect"]')).toHaveCount(3);
  await expect(tl.getByTestId('timeline-undated')).toContainText('Rivalités impériales');
});

test('candidate space: OFF by default (never requested), labelled and visually distinct when ON, never in the graph', async ({ page }) => {
  const calls: string[] = [];
  await installFixtures(page, calls);
  await page.goto('/v3/causes?event=Q361');
  await noLoading(page);
  const toggle = page.getByTestId('candidates-toggle');
  await expect(toggle).not.toBeChecked();
  expect(calls.some((c) => c.includes('/v1/causal/candidates'))).toBe(false);
  await toggle.check();
  const panel = page.getByTestId('candidates-panel');
  await expect(panel).toContainText('candidats extraits de textes — non sourcés, non vérifiés');
  await expect(panel.getByTestId('candidate')).toHaveCount(2);
  await expect(panel.getByTestId('grade-badge')).toHaveCount(0);
  expect(await panel.evaluate((el) => getComputedStyle(el).borderTopStyle)).toBe('dashed');
  await expect(page.getByTestId('causal-canvas')).toHaveAttribute('data-edges', '10');
});

test('chain finder: banner, sourced hops, weakest-link grade; no chain is not "no link"', async ({ page }) => {
  await installFixtures(page);
  await page.goto('/v3/causes?event=Q361&de=fixture%3Asarajevo_1914&vers=fixture%3Aversailles');
  const finder = page.getByTestId('chain-finder');
  await expect(finder.getByTestId('chain-banner')).toContainText('Chaîne composée par REVA à partir d’affirmations distinctes — force = maillon le plus faible');
  const chain = finder.getByTestId('causal-chain');
  await expect(chain.getByTestId('chain-hop')).toHaveCount(2);
  await expect(chain.getByTestId('grade-badge').first()).toHaveAttribute('data-grade', 'C');
  await page.goto('/v3/causes?event=Q361&vers=fixture%3Aunrelated');
  await expect(page.getByTestId('chain-none')).toContainText('Cela ne prouve ni l’absence de lien causal, ni son existence.');
});

test('causal API not deployed: "pas encore disponible", nothing drawn', async ({ page }) => {
  await installFixtures(page);
  await page.route(/\/v1\/causal\//, (route) => json(route, F.NOT_FOUND_ROUTE, 404));
  await page.goto('/v3/causes?event=Q361');
  await expect(page.getByText('Pas encore disponible').first()).toBeVisible();
  await expect(page.getByTestId('causal-canvas')).toHaveCount(0);
  await expect(page.getByTestId('claim-line')).toHaveCount(0);
});

test('Demander: causal answers name source and tier in every causal sentence; the chain answer says it is a composition', async ({ page }) => {
  await installFixtures(page);
  await page.goto('/v3/demander');
  await page.getByLabel('Votre question').fill('Pourquoi la Première Guerre mondiale a-t-elle éclaté ?');
  await page.getByRole('button', { name: 'Demander' }).click();
  const causal = page.locator('[data-testid="answer-sentence"][data-kind="causal"]');
  await expect(causal.first()).toBeVisible();
  for (const s of await causal.all()) await expect(s).toHaveText(/^Selon .+ \(T[1-4], /);
  await page.getByLabel('Votre question').fill('Chaîne causale entre l’attentat de Sarajevo et le traité de Versailles ?');
  await page.getByRole('button', { name: 'Demander' }).click();
  await expect(page.getByTestId('ask-answer')).toContainText('Chaîne composée par REVA à partir de 2 affirmation(s) distincte(s)');
});

test('palette opens the screen; 375 px has no horizontal scroll', async ({ page }) => {
  await installFixtures(page);
  await page.goto('/v3');
  await expect(page.getByRole('heading', { name: /La planète, reliée/ })).toBeVisible();
  await page.keyboard.press('Control+k');
  await page.getByRole('combobox', { name: /Search/ }).fill('Causes & conséquences — Première');
  await page.keyboard.press('Enter');
  await expect(page).toHaveURL(/\/v3\/causes\?event=Q361/);
  await page.setViewportSize({ width: 375, height: 800 });
  await page.goto('/v3/causes?event=Q361&de=fixture%3Asarajevo_1914&vers=fixture%3Aversailles');
  await noLoading(page);
  await page.getByTestId('candidates-toggle').check();
  await noLoading(page);
  const overflow = await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
  expect(overflow).toBeLessThanOrEqual(1);
});
