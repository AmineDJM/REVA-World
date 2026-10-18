/**
 * V8 console shell end to end (frame, palette, go-shortcuts, theme, phone layout, old-nav link, performance).
 *
 * Two ways to run:
 *  - against the e2e stack (real session + built UI):   uv run python scripts/e2e_stack.py --run -- e2e/v8_shell.spec.ts
 *  - standalone: build the UI (`npm run build`), serve it with `uv run reva api --port 8101` (or `npx vite preview`), then
 *      REVA_E2E_V8_URL=http://127.0.0.1:8101 PLAYWRIGHT_BROWSERS_PATH=/opt/pw-browsers npx playwright test e2e/v8_shell.spec.ts
 *    The session and the search endpoints are intercepted with the V3 fixtures (labelled test_fixture); every other
 *    /v1 call answers "route absent" (404), so feature pages show their "pas encore disponible" states.
 * REVA_E2E_SHOTS=<dir> also writes light / dark / phone screenshots there.
 * Performance numbers (LCP, route switch) are printed and attached as test annotations.
 */
import { expect, test, type Page, type Route } from '@playwright/test';
import { BASE_URL, HAVE_SESSION, STORAGE_STATE } from './helpers';
import * as F from '../src/v3/test/fixtures';

const STANDALONE_URL = process.env.REVA_E2E_V8_URL ?? '';
const STANDALONE = !HAVE_SESSION && !!STANDALONE_URL;
const BASE = HAVE_SESSION ? BASE_URL : STANDALONE_URL;
const SHOTS = process.env.REVA_E2E_SHOTS ?? '';

test.skip(!BASE, 'No REVA e2e stack and no REVA_E2E_V8_URL (built UI served by `uv run reva api` or vite preview)');
test.use({ baseURL: BASE, ...(HAVE_SESSION ? { storageState: STORAGE_STATE } : {}) });

const json = (route: Route, body: unknown, status = 200) =>
  route.fulfill({ status, contentType: status >= 400 ? 'application/problem+json' : 'application/json', body: JSON.stringify(body) });

async function install(page: Page) {
  await page.route(/\/v1\//, async (route) => {
    const req = route.request();
    const p = decodeURIComponent(new URL(req.url()).pathname);
    if (p === '/v1/session' && req.method() === 'GET') {
      return STANDALONE ? json(route, { username: 'e2e', principal: 'user:e2e', auth_method: 'session', scopes: ['read'] }) : route.fallback();
    }
    if (!STANDALONE) return route.fallback();
    if (p === '/v1/v8/search') return json(route, F.NOT_FOUND_ROUTE, 404); // exercise the fallback
    if (p === '/v1/kg/search') return json(route, F.SEARCH_BRESIL);
    if (p === '/v1/atlas/concepts') return json(route, F.CONCEPTS);
    return json(route, F.NOT_FOUND_ROUTE, 404);
  });
}

test.beforeEach(async ({ page }) => {
  await install(page);
});

/** Navigate and wait for the console frame (session check + lazy shell chunk). */
async function open(page: Page, url: string) {
  await page.goto(url);
  await expect(page.locator('main.v8-main')).toBeVisible({ timeout: 20_000 });
}

async function shot(page: Page, name: string) {
  if (SHOTS) await page.screenshot({ path: `${SHOTS}/${name}.png`, fullPage: false });
}

async function noHorizontalScroll(page: Page) {
  const over = await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
  expect(over, 'horizontal overflow (px)').toBeLessThanOrEqual(0);
}

test('frame: rail lists the discovered features, breadcrumbs, data status, old screens link', async ({ page }) => {
  await open(page, '/v8');
  const rail = page.getByRole('navigation', { name: 'Sections' });
  await expect(rail).toBeVisible();
  expect(await rail.getByRole('link').count()).toBeGreaterThan(0);
  await expect(page.getByRole('navigation', { name: 'Fil d’Ariane' })).toContainText('Console');
  await expect(page.getByTestId('v8-health')).toHaveAttribute('data-status', /ready|not_ready|unreachable/);
  await expect(page.getByRole('link', { name: /Anciens écrans/ })).toHaveAttribute('href', '/world');
  await shot(page, 'v8-light-home');
});

test('palette: Ctrl-K, grouped fallback results, keyboard navigation, Enter navigates, recents', async ({ page }) => {
  await open(page, '/v8');
  await page.keyboard.press('Control+k');
  const dialog = page.getByRole('dialog', { name: /Palette/ });
  await expect(dialog).toBeVisible();
  await expect(dialog.getByRole('group').first()).toBeVisible(); // "Aller à" with the features
  await page.keyboard.type('bré');
  if (STANDALONE) {
    await expect(dialog.getByRole('option', { name: /Brésil/ }).first()).toBeVisible();
    await expect(dialog).toContainText('recherche de secours');
  }
  await shot(page, 'v8-light-palette');
  await page.keyboard.press('ArrowDown');
  await page.keyboard.press('ArrowUp');
  const target = await dialog.locator('[role=option][aria-selected=true]').count();
  expect(target).toBe(1);
  await page.keyboard.press('Enter');
  await expect(dialog).toBeHidden();
  await expect(page).toHaveURL(/\/v8\//);
  // reopen with "/": recents first
  await page.locator('body').click({ position: { x: 600, y: 400 } }).catch(() => undefined);
  await page.keyboard.press('Escape');
  await page.keyboard.press('/');
  await expect(page.getByRole('dialog', { name: /Palette/ })).toContainText('Récents');
  await page.keyboard.press('Escape');
});

test('g <key> shortcuts, shortcuts sheet, theme toggle', async ({ page }) => {
  await open(page, '/v8');
  await page.keyboard.press('Shift+?');
  const help = page.getByRole('dialog', { name: 'Raccourcis clavier' });
  await expect(help).toBeVisible();
  const goRows = await help.locator('.v8-help-row dd').allTextContents();
  await page.keyboard.press('Escape');
  const links = page.getByRole('navigation', { name: 'Sections' }).getByRole('link');
  const n = await links.count();
  for (let i = 0; i < n; i++) {
    const href = await links.nth(i).getAttribute('href');
    const key = await links.nth(i).locator('.v8-kbd').nth(1).textContent().catch(() => null);
    if (!href || !key || href === '/v8') continue;
    await page.keyboard.press('g');
    await page.keyboard.press(key);
    await expect(page).toHaveURL(new RegExp(`${href}$`));
    break;
  }
  expect(goRows.length).toBeGreaterThan(0);
  await page.getByTestId('v8-theme-toggle').click();
  const theme = await page.evaluate(() => document.documentElement.dataset.theme);
  expect(['dark', 'light']).toContain(theme);
  await page.emulateMedia({ colorScheme: 'light' });
  await open(page, '/v8');
  await page.evaluate(() => localStorage.setItem('v8.theme', 'dark'));
  await page.reload();
  await expect(page.locator('main.v8-main')).toBeVisible();
  await expect(page.locator('html')).toHaveAttribute('data-theme', 'dark');
  const bg = await page.evaluate(() => getComputedStyle(document.querySelector('.v8-shell')!).backgroundColor);
  expect(bg).toBe('rgb(14, 16, 19)');
  await shot(page, 'v8-dark-home');
  await page.keyboard.press('Control+k');
  await page.keyboard.type('bré');
  await page.waitForTimeout(400);
  await shot(page, 'v8-dark-palette');
  await page.evaluate(() => localStorage.removeItem('v8.theme'));
});

test.describe('phone', () => {
  test.use({ viewport: { width: 375, height: 760 }, isMobile: true, hasTouch: true });
  test('rail becomes a bottom sheet, 16 px gutters, no horizontal scroll', async ({ page }) => {
    await open(page, '/v8');
    await expect(page.getByRole('navigation', { name: 'Sections' })).toBeHidden();
    await noHorizontalScroll(page);
    const pad = await page.evaluate(() => getComputedStyle(document.querySelector('main.v8-main')!).paddingLeft);
    expect(pad).toBe('16px');
    await shot(page, 'v8-phone-home');
    await page.getByRole('button', { name: 'Ouvrir la navigation' }).click();
    const sheet = page.getByRole('dialog', { name: 'Navigation' });
    await expect(sheet).toBeVisible();
    await shot(page, 'v8-phone-sheet');
    const first = sheet.getByRole('link').first();
    const href = await first.getAttribute('href');
    await first.click();
    await expect(sheet).toBeHidden();
    await expect(page).toHaveURL(new RegExp(`${href}$`));
    await noHorizontalScroll(page);
    await page.getByRole('button', { name: /Rechercher/ }).click();
    await expect(page.getByRole('dialog', { name: /Palette/ })).toBeVisible();
    await shot(page, 'v8-phone-palette');
  });
});

test('old screens keep working at / with a visible "Nouvelle console V8" link', async ({ page }) => {
  await page.goto('/world');
  const link = page.getByRole('link', { name: /Nouvelle console V8/ });
  await expect(link).toBeVisible();
  await link.click();
  await expect(page).toHaveURL(/\/v8$/);
  await expect(page.getByRole('navigation', { name: 'Sections' })).toBeVisible();
});

test('performance: LCP of /v8 and route switch time (built UI)', async ({ page }, info) => {
  await page.goto('/v8', { waitUntil: 'load' });
  const lcp = await page.evaluate(
    () =>
      new Promise<number>((resolve) => {
        let v = 0;
        new PerformanceObserver((l) => {
          for (const e of l.getEntries()) v = Math.max(v, e.startTime);
        }).observe({ type: 'largest-contentful-paint', buffered: true });
        setTimeout(() => resolve(v), 800);
      }),
  );
  const cls = await page.evaluate(
    () =>
      new Promise<number>((resolve) => {
        let v = 0;
        new PerformanceObserver((l) => {
          for (const e of l.getEntries() as unknown as { value: number; hadRecentInput: boolean }[]) if (!e.hadRecentInput) v += e.value;
        }).observe({ type: 'layout-shift', buffered: true });
        setTimeout(() => resolve(v), 300);
      }),
  );
  const links = page.getByRole('navigation', { name: 'Sections' }).getByRole('link');
  const switches: { href: string; ms: number }[] = [];
  const n = await links.count();
  for (let i = 0; i < n; i++) {
    const link = links.nth(i);
    const href = (await link.getAttribute('href'))!;
    await link.hover(); // prefetch
    await page.waitForTimeout(150);
    const t0 = await page.evaluate(() => performance.now());
    await link.click();
    await expect(page).toHaveURL(new RegExp(`${href}$`));
    // route rendered = the shell's suspense skeleton is gone
    await expect(page.locator('main.v8-main [aria-busy=true]')).toHaveCount(0, { timeout: 15_000 });
    const t1 = await page.evaluate(() => performance.now());
    switches.push({ href, ms: Math.round(t1 - t0) });
  }
  const report = { lcp_ms: Math.round(lcp), cls: Number(cls.toFixed(4)), route_switch_ms: switches };
  console.log('V8 perf', JSON.stringify(report));
  info.annotations.push({ type: 'v8-perf', description: JSON.stringify(report) });
  expect(lcp).toBeLessThan(2500);
  expect(cls).toBeLessThan(0.1);
});
