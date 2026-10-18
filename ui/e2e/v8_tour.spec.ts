/**
 * V8 console tour on REAL data (V8QA): every feature with real ids of the dev database — no interception.
 *
 * Needs a running API serving the built UI (`npm run build`) on the dev database and a signed-in session:
 *   - the e2e stack (`uv run python scripts/e2e_stack.py --run -- e2e/v8_tour.spec.ts`) gives a session but only
 *     fixture data, so most entity pages answer « introuvable » (the spec still checks frame, overflow, errors);
 *   - against the dev database: start `uv run reva api` with a per-run admin password (Argon2id hash in the process
 *     environment only, as scripts/e2e_stack.py does), sign in once over HTTP into a storageState file, then
 *       REVA_E2E_BASE_URL=http://127.0.0.1:8190 REVA_E2E_STORAGE_STATE=<state.json> REVA_E2E_TOUR_LIVE=1 \
 *       PLAYWRIGHT_BROWSERS_PATH=/opt/pw-browsers npx playwright test e2e/v8_tour.spec.ts
 * REVA_E2E_SHOTS=<dir> writes light / dark / phone screenshots of every stop.
 *
 * For each stop: the key content appears (answer first), no console error / page error / 5xx, no horizontal scroll
 * at 390 px, and the time from navigation to the key content is recorded (printed as JSON, attached as annotations).
 */
import { expect, test, type ConsoleMessage, type Page } from '@playwright/test';
import { BASE_URL, HAVE_SESSION, STORAGE_STATE } from './helpers';

const LIVE = process.env.REVA_E2E_TOUR_LIVE === '1';
const SHOTS = process.env.REVA_E2E_SHOTS ?? '';

test.skip(!HAVE_SESSION, 'No signed-in V8 stack (REVA_E2E_BASE_URL + REVA_E2E_STORAGE_STATE)');
test.use({ baseURL: BASE_URL, storageState: STORAGE_STATE || undefined });
test.describe.configure({ mode: 'serial' });

interface Stop {
  name: string;
  url: string;
  /** Key content (answer first) that must be visible; on the fixture stack only the h1 is required. */
  key: RegExp;
  /** Optional locator that must be visible once loaded (live data only). */
  ready?: string;
  timeout?: number;
}

export const STOPS: Stop[] = [
  { name: 'accueil', url: '/v8', key: /Le pouls du monde/ },
  { name: 'fiche-pays', url: '/v8/fiche/country/FRA', key: /France/, ready: '.fiche-title' },
  { name: 'fiche-petrole', url: '/v8/fiche/commodity/crude_oil', key: /[Pp]étrole|Brent/, ready: '.fiche-title' },
  { name: 'fiche-medicament', url: '/v8/fiche/drug/chembl:CHEMBL941', key: /[Ii]matinib|IMATINIB/, ready: '.fiche-title' },
  { name: 'fiche-conflit', url: '/v8/fiche/armed_conflict/ucdp:conflict:13243', key: /Russi|Ukrain/, ready: '.fiche-title' },
  { name: 'fiche-maladie', url: '/v8/fiche/disease/mondo:0005136', key: /[Pp]aludisme|[Mm]alaria/, ready: '.fiche-title' },
  { name: 'monde', url: '/v8/monde?table=wb.wdi_econ&measure=NY.GDP.PCAP.CD', key: /PIB par habitant/, ready: '[data-testid=monde-answer]' },
  { name: 'monde-inflation', url: '/v8/monde?table=imf.weo&measure=PCPIPCH', key: /Inflation/, ready: '[data-testid=monde-map]' },
  { name: 'comparer', url: '/v8/comparer?s=imf.weo.PCPIPCH&s=wb.wdi_econ.NY.GDP.PCAP.CD&g=FRA,DEU,ITA', key: /Comparer/, ready: '.v8c-panel-head' },
  { name: 'liens', url: '/v8/liens?from=chembl:CHEMBL941&to=efo:0000339', key: /Liens/, ready: '[data-testid=lien-answer]', timeout: 60_000 },
  { name: 'demander', url: `/v8/demander?q=${encodeURIComponent('Quel est le prix du Brent ?')}`, key: /Demander/, ready: '.v8d-answer, [data-testid=answer-card], article', timeout: 60_000 },
  { name: 'demander-brainstorm', url: `/v8/demander?mode=brainstorm&t=${encodeURIComponent('prévoir le prix du Brent')}`, key: /Demander/, ready: '.v8b-verdict, .v8d-brainstorm, section', timeout: 60_000 },
  { name: 'decision', url: '/v8/decision?ref=opec_plus_cut_2mbd_2027q1', key: /Décision/, ready: '.v8-page h2', timeout: 90_000 },
  { name: 'scores', url: '/v8/scores', key: /Scores/, ready: '[data-testid=headline]' },
  { name: 'scores-piste', url: '/v8/scores?track=beat_energy', key: /Scores/, ready: '[data-testid=track-view]' },
  { name: 'tableaux', url: '/v8/tableaux', key: /[Tt]ableaux|[Éé]pingles/ },
  { name: 'couverture', url: '/v8/couverture', key: /Couverture/ },
  { name: 'dossier-index', url: '/v8/dossier', key: /Dossier/ },
  { name: 'dossier-pays', url: '/v8/dossier/country/FRA', key: /France/, ready: '#dos-title', timeout: 120_000 },
  { name: 'dossier-petrole', url: '/v8/dossier/commodity/crude_oil', key: /[Pp]étrole/, ready: '#dos-title', timeout: 120_000 },
  { name: 'dossier-paludisme', url: '/v8/dossier/disease/malaria', key: /[Pp]aludisme/, ready: '#dos-title', timeout: 120_000 },
];

/** Console noise that is not an application error: software-WebGL notices of headless Chromium. */
const NOISE = [/WebGL/i, /GPU stall/i, /swiftshader/i, /GroupMarkerNotSet/];

function watch(page: Page) {
  const errors: string[] = [];
  page.on('console', (m: ConsoleMessage) => {
    if (m.type() !== 'error') return;
    const t = m.text();
    if (!NOISE.some((r) => r.test(t))) errors.push(`console: ${t.slice(0, 240)}`);
  });
  page.on('pageerror', (e) => errors.push(`pageerror: ${e.message.slice(0, 240)}`));
  page.on('response', (r) => {
    if (r.status() >= 500 && new URL(r.url()).pathname.startsWith('/v1/')) errors.push(`HTTP ${r.status()} ${new URL(r.url()).pathname}`);
  });
  return errors;
}

async function overflowPx(page: Page) {
  return page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
}

/** Visible interactive elements without an accessible name (aria-label/labelledby, <label>, text, title, placeholder). */
async function unnamed(page: Page): Promise<string[]> {
  return page.evaluate(() => {
    const out: string[] = [];
    const sel = 'button, a[href], input:not([type=hidden]), select, textarea, [role=tab], [role=button], [role=combobox]';
    for (const el of Array.from(document.querySelectorAll<HTMLElement>(sel))) {
      if (!el.offsetParent && getComputedStyle(el).position !== 'fixed') continue;
      const by = el.getAttribute('aria-labelledby');
      const labelled = by ? by.split(/\s+/).map((id) => document.getElementById(id)?.textContent ?? '').join(' ') : '';
      const labels = (el as HTMLInputElement).labels ? Array.from((el as HTMLInputElement).labels ?? []).map((l) => l.textContent ?? '').join(' ') : '';
      const name = [el.getAttribute('aria-label'), labelled, labels, el.textContent, el.getAttribute('title'), el.getAttribute('placeholder'), el.querySelector('img')?.getAttribute('alt')]
        .map((x) => (x ?? '').trim())
        .find(Boolean);
      if (!name) out.push(`${el.tagName.toLowerCase()}${el.className ? '.' + String(el.className).split(' ')[0] : ''}`);
    }
    return out;
  });
}

const timings: Record<string, number> = {};

for (const s of STOPS) {
  test(`${s.name} — ${s.url}`, async ({ page }, info) => {
    const timeout = s.timeout ?? 45_000;
    test.setTimeout(timeout * 2 + 30_000);
    const errors = watch(page);
    await page.setViewportSize({ width: 1440, height: 900 });
    const t0 = Date.now();
    await page.goto(s.url);
    await expect(page.locator('main.v8-main')).toBeVisible({ timeout: 30_000 });
    await expect(page.locator('main.v8-main')).toContainText(s.key, { timeout });
    if (LIVE && s.ready) await expect(page.locator(s.ready).first()).toBeVisible({ timeout });
    const ms = Date.now() - t0;
    timings[s.name] = ms;
    info.annotations.push({ type: 'timing', description: `${s.name}: ${ms} ms` });
    expect(await unnamed(page), 'interactive elements without an accessible name').toEqual([]);
    // honesty: no rendering artefact where a value should be (a missing value is typed, never NaN/undefined; « null » can be quoted from a source)
    expect(await page.locator('main.v8-main').innerText()).not.toMatch(/\bNaN\b|\bundefined\b|\[object Object\]|\bInfinity\b/);
    // the error boundary never fired
    await expect(page.getByText('Cette section a échoué')).toHaveCount(0);
    if (SHOTS) {
      await page.waitForTimeout(800);
      await page.screenshot({ path: `${SHOTS}/${s.name}-light.png` });
      await page.emulateMedia({ colorScheme: 'dark' });
      await page.waitForTimeout(400);
      await page.screenshot({ path: `${SHOTS}/${s.name}-dark.png` });
      await page.emulateMedia({ colorScheme: 'light' });
    }
    // phone: 390 px, no horizontal scroll
    await page.setViewportSize({ width: 390, height: 844 });
    await page.waitForTimeout(500);
    expect(await overflowPx(page), 'horizontal overflow at 390 px').toBeLessThanOrEqual(0);
    if (SHOTS) await page.screenshot({ path: `${SHOTS}/${s.name}-phone.png` });
    expect(errors, 'console errors, page errors, 5xx').toEqual([]);
  });
}

test('keyboard only: palette, go-keys, nav and tabs', async ({ page }) => {
  const errors = watch(page);
  await page.setViewportSize({ width: 1440, height: 900 });
  await page.goto('/v8');
  await expect(page.locator('main.v8-main')).toContainText(/Le pouls du monde/, { timeout: 30_000 });
  // palette: Ctrl-K, type, Enter
  await page.keyboard.press('Control+k');
  const box = page.getByRole('combobox');
  await expect(box).toBeFocused();
  await page.keyboard.type('Scores');
  await expect(page.getByRole('option').first()).toBeVisible({ timeout: 15_000 });
  await page.keyboard.press('Enter');
  await expect(page).toHaveURL(/\/v8\/scores/);
  // go-keys
  await page.locator('body').click({ position: { x: 5, y: 5 } }).catch(() => undefined);
  await page.keyboard.press('Escape');
  await page.keyboard.press('g');
  await page.keyboard.press('m');
  await expect(page).toHaveURL(/\/v8\/monde/);
  // Tab reaches the nav links with a visible focus ring
  await page.goto('/v8');
  await expect(page.locator('main.v8-main')).toBeVisible({ timeout: 30_000 });
  let ring = false;
  for (let i = 0; i < 12 && !ring; i++) {
    await page.keyboard.press('Tab');
    ring = await page.evaluate(() => {
      const a = document.activeElement as HTMLElement | null;
      if (!a || !a.closest('nav')) return false;
      const cs = getComputedStyle(a);
      return (cs.outlineStyle !== 'none' && cs.outlineWidth !== '0px') || cs.boxShadow !== 'none';
    });
  }
  expect(ring, 'a nav link gets a visible focus ring with Tab').toBe(true);
  // Fiche section tabs are reachable with j/k (keyboard shortcuts) and the URL keeps the state
  await page.goto('/v8/fiche/country/FRA');
  await expect(page.locator('.fiche-title')).toBeVisible({ timeout: 45_000 });
  await page.keyboard.press('j');
  await expect(page.locator('main.v8-main :focus')).toHaveCount(1);
  expect(errors).toEqual([]);
});

test('/ opens the V8 console; old screens stay reachable (/world, « Anciens écrans »)', async ({ page }) => {
  const errors = watch(page);
  await page.goto('/');
  await expect(page).toHaveURL(/\/v8$/);
  await expect(page.locator('main.v8-main')).toContainText(/Le pouls du monde/, { timeout: 30_000 });
  const old = page.getByRole('link', { name: /Anciens écrans/ }).first();
  await expect(old).toHaveAttribute('href', '/world');
  await page.goto('/world');
  await expect(page.locator('header.app-header')).toBeVisible({ timeout: 30_000 });
  await expect(page.getByRole('link', { name: /Nouvelle console V8/ })).toBeVisible();
  await page.goto('/v3/');
  await expect(page).toHaveURL(/\/v3\//);
  expect(errors).toEqual([]);
});

test.afterAll(() => {
  console.log(`V8_TOUR_TIMINGS ${JSON.stringify(timings)}`);
});
