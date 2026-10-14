/**
 * Quick smoke checks (shared stored session; see helpers.ts for how the stack is found).
 * The detailed acceptance checks live in auth/screens/country/simulation specs.
 */
import { expect, test } from '@playwright/test';
import { BASE_URL, HAVE_SESSION, SKIP_REASON, STORAGE_STATE, settle } from './helpers';

test.skip(!HAVE_SESSION, SKIP_REASON);
test.use({ baseURL: BASE_URL, storageState: STORAGE_STATE });

test('health endpoints answer and security headers are set on the UI shell', async ({ page }) => {
  expect((await page.request.get('/health/live')).status()).toBe(200);
  expect((await page.request.get('/health/ready')).status()).toBe(200);
  const r = await page.goto('/world');
  const h = r!.headers();
  expect(h['content-security-policy']).toContain("default-src 'self'");
  expect(h['x-frame-options']).toBe('DENY');
  expect(h['x-content-type-options']).toBe('nosniff');
});

test('map notes that display boundaries are not authoritative', async ({ page }) => {
  await page.goto('/world');
  await settle(page);
  await expect(page.getByText(/display-only/).first()).toBeVisible();
});

test('no external network requests are made by the UI (self-hosted map, no tile service)', async ({ page }) => {
  const external: string[] = [];
  page.on('request', (r) => {
    const u = new URL(r.url());
    if (!['127.0.0.1', 'localhost'].includes(u.hostname) && !['data:', 'blob:'].includes(u.protocol)) external.push(r.url());
  });
  for (const path of ['/world', '/country/FRA', '/graph']) {
    await page.goto(path);
    await settle(page);
  }
  expect(external).toEqual([]);
});

test('unknown screen and API 404 render clearly', async ({ page }) => {
  await page.goto('/no-such-screen');
  await expect(page.getByText('Unknown screen')).toBeVisible();
  const r = await page.request.get('/v1/no-such-endpoint');
  expect(r.status()).toBe(404);
  expect(r.headers()['content-type']).toContain('application/problem+json');
});
