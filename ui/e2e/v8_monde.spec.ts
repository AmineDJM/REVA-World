/**
 * V8 Monde + Comparer end to end (owner V8MONDE), against the e2e stack with the V3 atlas fixtures:
 *   uv run python scripts/e2e_stack.py --v3-fixtures --run -- e2e/v8_monde.spec.ts
 * The series are discovered through /v1/v8/series/catalog (whatever the fixture snapshot holds), so nothing is
 * hard-coded. REVA_E2E_SHOTS=<dir> writes map light/dark/phone, bivariate and compare screenshots.
 */
import { expect, test, type Page } from '@playwright/test';
import { BASE_URL, HAVE_SESSION, STORAGE_STATE } from './helpers';

const SHOTS = process.env.REVA_E2E_SHOTS ?? '';
test.skip(!HAVE_SESSION, 'needs the e2e stack (scripts/e2e_stack.py --v3-fixtures --run -- e2e/v8_monde.spec.ts)');
test.use({ baseURL: BASE_URL, storageState: STORAGE_STATE });

interface Hit {
  series: string | null;
  in_snapshot: boolean;
  table: string | null;
}

async function seriesFor(page: Page, queries: string[], n: number): Promise<string[]> {
  const out: string[] = [];
  for (const q of queries) {
    const r = await page.request.get(`/v1/v8/series/catalog?q=${encodeURIComponent(q)}&limit=20`);
    if (!r.ok()) continue;
    const body = (await r.json()) as { hits: Hit[] };
    for (const h of body.hits) {
      if (!h.series || !h.in_snapshot || out.includes(h.series)) continue;
      const m = await page.request.get(`/v1/v8/series/map?series=${encodeURIComponent(h.series)}`);
      if (!m.ok()) continue;
      const d = (await m.json()) as { data: { counts: { values?: number } } };
      if ((d.data.counts.values ?? 0) >= 5) {
        out.push(h.series);
        break;
      }
    }
    if (out.length >= n) break;
  }
  return out;
}

async function shot(page: Page, name: string, full = true) {
  if (SHOTS) await page.screenshot({ path: `${SHOTS}/${name}.png`, fullPage: full });
}

async function noHorizontalScroll(page: Page) {
  const over = await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
  expect(over, 'horizontal overflow (px)').toBeLessThanOrEqual(0);
}

test('Monde: map, ranking, missing reasons, place panel, light/dark/phone', async ({ page }) => {
  test.setTimeout(240_000);
  const [x] = await seriesFor(page, ['PIB', 'chômage', 'inflation', 'emploi', 'espérance de vie', 'indice'], 1);
  expect(x, 'a fixture series with values').toBeTruthy();
  await page.goto(`/v8/monde?series=${encodeURIComponent(x!)}`);
  await expect(page.getByTestId('monde-answer')).toBeVisible({ timeout: 60_000 });
  await expect(page.getByTestId('monde-legend')).toContainText(/jamais rapporté|pas de cellule|non rapporté/);
  await page.waitForTimeout(2500); // map tiles
  await shot(page, 'monde-light');
  // first ranking row → place panel
  await page.locator('[aria-label="Classement"] tr.v8-vt-link').first().locator('td').first().click();
  await expect(page.getByTestId('place-panel')).toBeVisible({ timeout: 30_000 });
  await expect(page).toHaveURL(/sel=[A-Z]{3}/);
  await page.waitForTimeout(800);
  await shot(page, 'monde-panel');
  await page.emulateMedia({ colorScheme: 'dark' });
  await page.evaluate(() => document.documentElement.setAttribute('data-theme', 'dark'));
  await page.waitForTimeout(1500);
  await shot(page, 'monde-dark');
  await page.evaluate(() => document.documentElement.removeAttribute('data-theme'));
  await page.emulateMedia({ colorScheme: 'light' });
  await page.setViewportSize({ width: 375, height: 812 });
  await page.waitForTimeout(1500);
  await noHorizontalScroll(page);
  await shot(page, 'monde-phone');
});

test('Monde bivariate and Comparer', async ({ page }) => {
  test.setTimeout(240_000);
  const two = await seriesFor(page, ['PIB', 'inflation', 'chômage', 'emploi', 'espérance de vie', 'indice', 'salaire'], 2);
  test.skip(two.length < 2, 'fewer than two fixture series with values');
  const [x, y] = two as [string, string];
  await page.goto(`/v8/monde?series=${encodeURIComponent(x)}&yseries=${encodeURIComponent(y)}&mode=bivarie`);
  await expect(page.getByTestId('monde-answer')).toBeVisible({ timeout: 60_000 });
  await page.waitForTimeout(3000);
  await shot(page, 'monde-bivariate');
  // Comparer of the first series for the countries that the map lists first
  const map = (await (await page.request.get(`/v1/v8/series/map?series=${encodeURIComponent(x)}`)).json()) as { data: { values: { iso3: string }[] } };
  const geos = map.data.values.slice(0, 3).map((v) => v.iso3);
  await page.goto(`/v8/comparer?g=${geos.join(',')}&s=${encodeURIComponent(x)}&s=${encodeURIComponent(y)}`);
  await expect(page.getByRole('heading', { level: 2 }).first()).toBeVisible({ timeout: 60_000 });
  await page.waitForTimeout(2000);
  await shot(page, 'comparer');
  await page.getByRole('tab', { name: 'Tableau' }).click();
  await expect(page).toHaveURL(/vue=tableau/);
  await shot(page, 'comparer-table');
  await page.setViewportSize({ width: 375, height: 812 });
  await page.waitForTimeout(800);
  await noHorizontalScroll(page);
  await shot(page, 'comparer-phone');
});
