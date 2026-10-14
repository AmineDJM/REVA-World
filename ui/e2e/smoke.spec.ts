import { expect, test, type Page } from '@playwright/test';

const BASE = process.env.REVA_E2E_BASE_URL;
const USER = process.env.REVA_E2E_USERNAME ?? 'admin';
const PASS = process.env.REVA_E2E_PASSWORD ?? '';

test.skip(!BASE, 'REVA_E2E_BASE_URL is not set');

async function login(page: Page) {
  await page.goto('/world');
  await page.getByLabel('Username').fill(USER);
  await page.getByLabel('Password').fill(PASS);
  await page.getByRole('button', { name: 'Sign in' }).click();
  await expect(page.getByRole('navigation', { name: 'Screens' })).toBeVisible();
}

test('session cookie is HttpOnly and nothing is kept in web storage', async ({ page, context }) => {
  await login(page);
  const cookies = await context.cookies();
  const session = cookies.find((c) => c.name === 'reva_session');
  expect(session?.httpOnly).toBe(true);
  const stored = await page.evaluate(() => [localStorage.length, sessionStorage.length]);
  expect(stored).toEqual([0, 0]);
});

test('every screen shows context and knowledge cutoff', async ({ page }) => {
  await login(page);
  for (const path of ['/world', '/country/FRA', '/country/DZA', '/evidence', '/graph', '/simulation', '/models', '/acquisition']) {
    await page.goto(path);
    await expect(page.getByRole('region', { name: 'Result context' }).first()).toBeVisible();
    await expect(page.getByText('Knowledge cutoff').first()).toBeVisible();
  }
});

test('simulation screen carries a persistent SIMULATION banner and never uses the fixture baseline', async ({ page }) => {
  await login(page);
  const requests: string[] = [];
  page.on('request', (r) => {
    if (r.method() === 'POST' && r.url().endsWith('/v1/scenarios')) requests.push(r.postData() ?? '');
  });
  await page.goto('/simulation');
  await expect(page.getByTestId('simulation-screen-banner')).toContainText('SIMULATION');
  await page.mouse.wheel(0, 2000);
  await expect(page.getByTestId('simulation-screen-banner')).toBeInViewport();
  for (const body of requests) expect(body).not.toContain('00000000-0000-4000-8000-000000000001');
});

test('map notes that display boundaries are not authoritative', async ({ page }) => {
  await login(page);
  await page.goto('/world');
  await expect(page.getByText(/display-only/).first()).toBeVisible();
});
