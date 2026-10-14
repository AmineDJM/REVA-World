/**
 * Every screen shows result context (modality, context, valid time, knowledge cutoff)
 * and visibly labels fixture-origin data; keyboard navigation and reduced motion.
 */
import { expect, test } from '@playwright/test';
import { BASE_URL, HAVE_SESSION, RELEASE, SCREENS, SKIP_REASON, STORAGE_STATE, settle } from './helpers';

test.skip(!HAVE_SESSION, SKIP_REASON);
test.use({ baseURL: BASE_URL, storageState: STORAGE_STATE });

for (const path of SCREENS) {
  test(`${path} shows context, knowledge cutoff and the TEST FIXTURE label`, async ({ page }) => {
    const failures: string[] = [];
    page.on('response', (r) => {
      if (new URL(r.url()).pathname.startsWith('/v1/') && r.status() >= 500) failures.push(`${r.status()} ${r.url()}`);
    });
    page.on('pageerror', (e) => failures.push(`pageerror: ${e.message}`));
    await page.goto(path);
    await expect(page.getByRole('navigation', { name: 'Screens' })).toBeVisible();
    await settle(page);

    const banner = page.getByRole('region', { name: 'Result context' }).first();
    await expect(banner).toBeVisible();
    for (const term of ['Modality', 'Context', 'Valid / reference time', 'Knowledge cutoff']) {
      await expect(banner.getByText(term, { exact: true })).toBeVisible();
    }
    // The cutoff is a stated timestamp, not "not stated" (the stack has a published release).
    const cutoff = banner.locator('dt', { hasText: 'Knowledge cutoff' }).locator('xpath=following-sibling::dd[1]');
    await expect(cutoff).not.toHaveText(/not stated|not loaded/);
    if (RELEASE) await expect(cutoff).toContainText(RELEASE.knowledge_cutoff.slice(0, 10));

    // All data in this stack is captured fixtures: the screen must say so.
    await expect(page.getByTestId('fixture-label').first()).toBeVisible();
    await expect(page.getByTestId('fixture-label').first()).toContainText('TEST FIXTURE');

    expect(failures, failures.join('\n')).toEqual([]);
  });
}

test('keyboard navigation reaches the skip link, every screen link and the main simulation controls', async ({ page }) => {
  await page.goto('/simulation');
  await settle(page);
  await page.locator('body').click({ position: { x: 1, y: 1 } });
  await page.keyboard.press('Tab');
  const first = await page.evaluate(() => document.activeElement?.textContent?.trim());
  expect(first).toBe('Skip to content');

  const reached: string[] = [];
  for (let i = 0; i < 80; i++) {
    const name = await page.evaluate(() => {
      const el = document.activeElement as HTMLElement | null;
      if (!el) return '';
      const label = el.getAttribute('aria-label') ?? (el as HTMLInputElement).labels?.[0]?.textContent ?? el.textContent ?? '';
      return `${el.tagName.toLowerCase()}:${label.trim().replace(/\s+/g, ' ').slice(0, 60)}`;
    });
    reached.push(name);
    if (name.startsWith('button:Create scenario and run')) break;
    await page.keyboard.press('Tab');
  }
  for (const label of ['World', 'Country', 'Evidence', 'Graph', 'Simulation', 'Models', 'Acquisition']) {
    expect(reached, `Tab order should reach the ${label} link`).toContain(`a:${label}`);
  }
  expect(reached.some((r) => r.startsWith('select:') || r.startsWith('input:'))).toBe(true);
  expect(reached.at(-1)).toMatch(/^button:Create scenario and run/);

  // Keyboard activation of a nav link changes screen.
  await page.goto('/world');
  await settle(page);
  await page.getByRole('link', { name: 'Models' }).focus();
  await page.keyboard.press('Enter');
  await expect(page).toHaveURL(/\/models$/);

  // Skip link moves focus to main content.
  await page.locator('body').click({ position: { x: 1, y: 1 } });
  await page.keyboard.press('Tab');
  await page.keyboard.press('Enter');
  expect(await page.evaluate(() => document.activeElement?.id)).toBe('main');
});

test('reduced motion is respected (no transitions or animations longer than a frame)', async ({ page }) => {
  await page.emulateMedia({ reducedMotion: 'reduce' });
  await page.goto('/world');
  await settle(page);
  expect(await page.evaluate(() => matchMedia('(prefers-reduced-motion: reduce)').matches)).toBe(true);
  const offenders = await page.evaluate(() => {
    const secs = (v: string) =>
      Math.max(0, ...v.split(',').map((x) => (x.trim().endsWith('ms') ? parseFloat(x) / 1000 : parseFloat(x) || 0)));
    const out: string[] = [];
    for (const el of Array.from(document.querySelectorAll<HTMLElement>('*'))) {
      const cs = getComputedStyle(el);
      const t = secs(cs.transitionDuration);
      const a = cs.animationName !== 'none' ? secs(cs.animationDuration) : 0;
      if (t > 0.01 || a > 0.01) out.push(`${el.tagName}.${el.className} t=${cs.transitionDuration} a=${cs.animationDuration}`);
    }
    return out.slice(0, 10);
  });
  expect(offenders).toEqual([]);
  // Smooth scrolling is disabled as well.
  expect(await page.evaluate(() => getComputedStyle(document.documentElement).scrollBehavior)).not.toBe('smooth');
});
