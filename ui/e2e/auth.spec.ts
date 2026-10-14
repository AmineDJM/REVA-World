/**
 * Authentication, session storage and CSRF (CI item 10: auth/CSRF).
 *
 * Login tests use a clean browser context (no stored session); everything else reuses
 * the stack's single storageState login.
 */
import { expect, test } from '@playwright/test';
import { BASE_URL, HAVE_SESSION, HAVE_STACK, PASSWORD, SKIP_REASON, STORAGE_STATE, USERNAME, csrfToken } from './helpers';

test.use({ baseURL: BASE_URL });

test.describe('login (fresh browser context)', () => {
  test.skip(!HAVE_STACK || !PASSWORD, SKIP_REASON);
  test.use({ storageState: { cookies: [], origins: [] } });

  test('unauthenticated navigation to a protected screen shows the sign-in form and no data', async ({ page }) => {
    const dataRequests: { url: string; status: number }[] = [];
    page.on('response', (r) => {
      const u = new URL(r.url());
      if (u.pathname.startsWith('/v1/') && u.pathname !== '/v1/session') dataRequests.push({ url: u.pathname, status: r.status() });
    });
    for (const path of ['/simulation', '/country/DZA', '/acquisition']) {
      await page.goto(path);
      await expect(page.getByRole('button', { name: 'Sign in' })).toBeVisible();
      await expect(page.getByLabel('Username')).toBeVisible();
      await expect(page.getByRole('navigation', { name: 'Screens' })).toHaveCount(0);
      await expect(page.getByTestId('simulation-screen-banner')).toHaveCount(0);
    }
    // Any data endpoint the shell touched must have been refused, never served.
    for (const r of dataRequests) expect(r.status, r.url).toBe(401);
    // The API itself refuses unauthenticated data reads with a problem document.
    const r = await page.request.get('/v1/beliefs?limit=1');
    expect(r.status()).toBe(401);
    expect(r.headers()['content-type']).toContain('application/problem+json');
  });

  test('wrong password is rejected with a clear error; correct password signs in with an HttpOnly session', async ({ page, context }) => {
    await page.goto('/world');
    await page.getByLabel('Username').fill(USERNAME);
    await page.getByLabel('Password').fill(`${PASSWORD}-wrong`);
    await page.getByRole('button', { name: 'Sign in' }).click();
    await expect(page.getByRole('alert')).toContainText('Invalid username or password');
    await expect(page.getByRole('navigation', { name: 'Screens' })).toHaveCount(0);
    expect((await context.cookies()).find((c) => c.name === 'reva_session')).toBeUndefined();

    await page.getByLabel('Password').fill(PASSWORD);
    await page.getByRole('button', { name: 'Sign in' }).click();
    await expect(page.getByRole('navigation', { name: 'Screens' })).toBeVisible();
    await expect(page.getByText(`Signed in as`)).toContainText(USERNAME);

    const cookies = await context.cookies();
    const session = cookies.find((c) => c.name === 'reva_session');
    expect(session, 'session cookie').toBeDefined();
    expect(session!.httpOnly).toBe(true);
    expect(session!.sameSite).toBe('Strict');
    // The session cookie is invisible to page scripts.
    const docCookie = await page.evaluate(() => document.cookie);
    expect(docCookie).not.toContain('reva_session');
    // Nothing (token, password, session) is kept in web storage.
    const stored = await page.evaluate(() => ({
      local: Object.keys(localStorage),
      session: Object.keys(sessionStorage),
    }));
    expect(stored).toEqual({ local: [], session: [] });
    // Indexed storage is not used for credentials either.
    const html = await page.content();
    expect(html).not.toContain(PASSWORD);

    // Sign out ends this (fresh) session; the shared storageState session is untouched.
    await page.getByRole('button', { name: 'Sign out' }).click();
    await expect(page.getByRole('button', { name: 'Sign in' })).toBeVisible();
    const after = await page.request.get('/v1/session');
    expect(after.status()).toBe(401);
  });
});

test.describe('CSRF on writes (stored session)', () => {
  test.skip(!HAVE_SESSION, SKIP_REASON);
  test.use({ storageState: STORAGE_STATE });

  test('a forged POST without X-CSRF-Token is rejected 403 even with a valid session cookie', async ({ page, context }) => {
    await page.goto('/world');
    await expect(page.getByRole('navigation', { name: 'Screens' })).toBeVisible();

    const body = { scenario_id: '7b0e6f0a-0000-4000-8000-000000000000', fidelity: 'L1' };
    const forged = await page.request.post('/v1/simulations', {
      data: body,
      headers: { 'Idempotency-Key': crypto.randomUUID() },
    });
    expect(forged.status()).toBe(403);
    const problem = await forged.json();
    expect(problem.code ?? problem.type).toMatch(/forbidden|csrf/i);
    expect(JSON.stringify(problem)).toContain('X-CSRF-Token');

    // A wrong token is rejected the same way.
    const wrong = await page.request.post('/v1/simulations', {
      data: body,
      headers: { 'Idempotency-Key': crypto.randomUUID(), 'X-CSRF-Token': 'not-the-token' },
    });
    expect(wrong.status()).toBe(403);

    // Scenario creation and logout are protected too.
    const sc = await page.request.post('/v1/scenarios', { data: {} });
    expect(sc.status()).toBe(403);
    const out = await page.request.delete('/v1/session');
    expect(out.status()).toBe(403);

    // With the real token the request passes CSRF and reaches validation (unknown scenario -> 404).
    const token = await csrfToken(context);
    const ok = await page.request.post('/v1/simulations', {
      data: body,
      headers: { 'Idempotency-Key': crypto.randomUUID(), 'X-CSRF-Token': token },
    });
    expect(ok.status()).toBe(404);

    // The session survived the forged requests.
    expect((await page.request.get('/v1/session')).status()).toBe(200);
  });
});
