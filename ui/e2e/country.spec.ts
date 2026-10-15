/**
 * Missing data is never a zero, and the provenance drawer reaches raw source bytes
 * (artifact SHA-256) and the raw selector (CI item 10: provenance, missing data).
 */
import { expect, test, type Locator } from '@playwright/test';
import { BASE_URL, HAVE_SESSION, SKIP_REASON, STORAGE_STATE, apiGet, settle } from './helpers';

test.skip(!HAVE_SESSION, SKIP_REASON);
test.use({ baseURL: BASE_URL, storageState: STORAGE_STATE });

const CRUDE_M = 'trade.hs2709.import_value.world_total';
const POP = 'demography.population.wdi_midyear_total';

function panel(page: import('@playwright/test').Page, title: string): Locator {
  return page.locator('section.panel').filter({ has: page.getByRole('heading', { name: title, level: 2 }) });
}

test('DZA crude imports are shown as missing with a reason, never as 0', async ({ page }) => {
  // Ground truth from the API: which DZA crude-import periods are typed Missing in the release.
  const beliefs = await apiGet(page.request, `/v1/beliefs?subject_id=DZA&concept=${CRUDE_M}&limit=1000`);
  const missing = (beliefs.data as any[]).filter((e) => e.value.kind === 'missing');
  expect(missing.length, 'fixture release holds missing DZA crude-import estimates').toBeGreaterThan(0);

  await page.goto('/country/DZA');
  await expect(page.getByRole('heading', { level: 1 })).toContainText('DZA');
  const crude = panel(page, 'Crude petroleum imports (HS 2709)');
  await expect(crude).toBeVisible();
  await expect(crude.locator('.state-loading')).toHaveCount(0, { timeout: 30_000 });

  // Context for this panel (belief release, cutoff) is shown.
  await expect(crude.getByRole('region', { name: 'Result context' })).toContainText('Knowledge cutoff');

  for (const e of missing) {
    const period = String(e.support.period_label ?? e.support.temporal?.label);
    const row = crude.getByRole('row').filter({ has: page.getByRole('rowheader', { name: period, exact: true }) });
    await expect(row, `row for ${period}`).toHaveCount(1);
    const value = row.getByRole('cell').first();
    const mv = value.locator('.missing-value');
    await expect(mv, `${period} renders as a missing value`).toBeVisible();
    await expect(mv).toHaveText(/^missing \(.+\)$/);
    await expect(mv).toHaveAttribute('data-missing-reason', String(e.value.reason));
    await expect(mv).not.toHaveText(/not stated/);
    // Never a zero, blank or dash.
    const text = (await value.innerText()).trim();
    expect(text).not.toMatch(/^(0|0\.0+|-|—|)$/);
    expect(text).not.toMatch(/(^|\s)0(\s|$)/);
  }
  await expect(crude.getByText(/Missing \(shown as gaps, not zero\)/)).toBeVisible();
  // The missing periods are named in the panel summary.
  const summary = crude.getByText(/Missing \(shown as gaps, not zero\)/);
  for (const e of missing) await expect(summary).toContainText(String(e.support.period_label));

  // Provider flags (Comtrade 1.2.0): a value is called a provider estimate only when
  // provider_value_estimated === true. legacyEstimationFlag codes quantity/net weight, never the
  // USD value, so the 2024 row (legacyEstimationFlag 6, isReported=false) must not be called an estimate.
  const obs = await apiGet(page.request, `/v1/observations?subject_id=DZA&concept=${CRUDE_M}&limit=1000`);
  const reported = (obs.data as any[]).filter((o) => o.value.kind !== 'missing' && !o.superseded_by_id);
  expect(reported.length, 'DZA has at least one reported crude-import value').toBeGreaterThan(0);
  for (const o of reported) {
    const period = String(o.support.period_label ?? o.support.temporal?.label);
    const row = crude.getByRole('row').filter({ has: page.getByRole('rowheader', { name: period, exact: true }) });
    if (!(await row.count())) continue;
    const f = o.source_flags ?? {};
    if (f.provider_value_estimated === true) {
      await expect(row.getByTestId('source-flag'), `${period} carries the provider value-estimate flag`).toContainText('provider estimate');
    } else {
      await expect(row, `${period}: legacyEstimationFlag ${f.legacyEstimationFlag} is not a value estimate`).not.toContainText('provider estimate');
      if (f.isReported === false) await expect(row.getByTestId('source-flag')).toContainText('not directly reported (isReported=false)');
    }
  }
});

test('country banner derives the data origin from the records shown (never "no records" next to TEST FIXTURE)', async ({ page }) => {
  const entity = await apiGet(page.request, '/v1/entities/FRA');
  await page.goto('/country/FRA');
  await settle(page);
  const banner = page.getByRole('region', { name: 'Result context' }).first();
  await expect(banner.getByTestId('fixture-label')).toContainText('TEST FIXTURE');
  const origin = banner.getByTestId('banner-origin');
  await expect(origin).toHaveAttribute('data-origin', 'test_fixture');
  await expect(origin).not.toContainText('no records');
  // Derived only because the entity endpoint itself reports none (backend gap, see docs/dev/ui_check.md).
  await expect(origin).toHaveAttribute('data-origin-derived', entity.origin === 'none' ? 'true' : 'false');
  // Every value listing carries the uncertainty glyph legend.
  const legends = page.getByTestId('uncertainty-legend');
  expect(await legends.count()).toBeGreaterThan(0);
  await expect(legends.first()).toContainText('Dotted line = uncertainty not quantified');
  await expect(legends.first()).toContainText('Hatched box = missing value');
});

test('charts render when the browser reports an invalid locale tag (en-US@posix)', async ({ page }) => {
  await page.addInitScript(() => {
    Object.defineProperty(Navigator.prototype, 'language', { configurable: true, get: () => 'en-US@posix' });
    Object.defineProperty(Navigator.prototype, 'languages', { configurable: true, get: () => ['en-US@posix'] });
  });
  const errors: string[] = [];
  page.on('pageerror', (e) => errors.push(String(e)));
  await page.goto('/country/FRA');
  await settle(page);
  const pop = panel(page, 'Population');
  await expect(pop.locator('.chart canvas').first()).toBeVisible({ timeout: 15_000 });
  await expect(page.getByText('Chart unavailable in this browser')).toHaveCount(0);
  expect(await page.evaluate(() => navigator.language)).toBe('en-US');
  expect(errors.filter((e) => /RangeError|locale/i.test(e))).toEqual([]);
});

test('provenance drawer from a belief value reaches the raw artifact SHA-256 and selector', async ({ page }) => {
  // Expected lineage from the API, for the first FRA population estimate.
  const beliefs = await apiGet(page.request, `/v1/beliefs?subject_id=FRA&concept=${POP}&limit=1000`);
  const est = (beliefs.data as any[]).find((e) => e.value.kind !== 'missing');
  expect(est).toBeDefined();
  const prov = await apiGet(page.request, `/v1/provenance/estimate/${est.id}`);
  const acq = (prov.data.nodes as any[]).find((n) => n.kind === 'acquisition');
  expect(acq, 'lineage contains the acquisition').toBeDefined();
  const rawSha: string = acq.attributes.raw_sha256;
  expect(rawSha).toMatch(/^[0-9a-f]{64}$/);
  const obs = (prov.data.nodes as any[]).find((n) => n.kind === 'observation');
  expect(obs?.attributes.raw_selector, 'observation carries a raw selector').toBeTruthy();

  await page.goto('/country/FRA');
  const pop = panel(page, 'Population');
  await expect(pop.locator('.state-loading')).toHaveCount(0, { timeout: 30_000 });
  const period = String(est.support.period_label ?? est.support.temporal?.label);
  const row = pop.getByRole('row').filter({ has: page.getByRole('rowheader', { name: period, exact: true }) });
  await row.getByRole('button', { name: 'Trace' }).click();

  const drawer = page.getByRole('dialog', { name: 'Provenance' });
  await expect(drawer).toBeVisible();
  await expect(drawer.locator('.state-loading')).toHaveCount(0, { timeout: 30_000 });
  await expect(drawer).toContainText(est.id);
  // Context of the lineage (release cutoff) is shown inside the drawer.
  await expect(drawer.getByRole('region', { name: 'Result context' })).toContainText('Knowledge cutoff');

  // The chain reaches the observation, the acquisition and the raw artifact.
  await expect(drawer.locator('.prov-observation')).toHaveCount(1);
  await expect(drawer.locator('.prov-acquisition')).toHaveCount(1);
  const raw = drawer.locator('.prov-artifact').filter({ hasText: rawSha });
  await expect(raw.first(), 'raw artifact node with its SHA-256').toBeVisible();
  await expect(raw.first().locator('dt', { hasText: 'SHA-256' })).toBeVisible();
  // The raw selector into those bytes is shown (edge selector on the artifact node and
  // the observation's raw_selector).
  await expect(raw.first().getByText(/^Selector \(selected from\)$/)).toBeVisible();
  await expect(raw.first()).toContainText(rawSha);
  await expect(raw.first()).toContainText('countryiso3code');
  const obsNode = drawer.locator('.prov-observation');
  await expect(obsNode.locator('dt', { hasText: 'Raw selector' })).toBeVisible();
  // The selected raw field: the observation's selector names the provider row it was read from.
  await expect(obsNode.locator('dt', { hasText: 'Raw selector' }).locator('xpath=following-sibling::dd[1]')).toContainText('countryiso3code');
  // Storage location of the bytes is reachable too.
  await expect(drawer.locator('.prov-storage_location').first()).toBeVisible();

  // Drill through to the source acquisition: its own lineage is rooted at the acquisition and
  // reaches the same raw bytes, the connector version and the registered source.
  const acqNode = drawer.locator('.prov-acquisition');
  await expect(acqNode.locator('dt', { hasText: 'SHA-256' }).locator('xpath=following-sibling::dd[1]')).toHaveText(rawSha);
  await acqNode.getByRole('button', { name: 'Trace from this acquisition' }).click();
  await expect(drawer.locator('.state-loading')).toHaveCount(0, { timeout: 30_000 });
  await expect(drawer.locator('.prov-node').first()).toHaveClass(/prov-acquisition/);
  await expect(drawer).toContainText(acq.id.replace(/^acquisition:/, ''));
  await expect(drawer.locator('.prov-artifact').filter({ hasText: rawSha }).first()).toBeVisible();
  await expect(drawer.locator('.prov-connector_version')).toHaveCount(1);
  await expect(drawer.locator('.prov-source')).toContainText('worldbank.wdi');
  await expect(drawer.locator('.prov-observation')).toHaveCount(0);
  await drawer.getByRole('button', { name: 'Back' }).click();
  await expect(drawer.locator('.state-loading')).toHaveCount(0, { timeout: 30_000 });
  await expect(drawer).toContainText(est.id);

  // Following the artifact keeps the SHA-256 (drill-down works), Escape closes and restores focus.
  await raw.first().getByRole('button', { name: /Trace from this artifact/ }).click();
  await expect(drawer.locator('.state-loading')).toHaveCount(0, { timeout: 30_000 });
  await expect(drawer).toContainText(rawSha);
  await drawer.getByRole('button', { name: 'Back' }).click();
  await page.keyboard.press('Escape');
  await expect(drawer).toHaveCount(0);
  await expect(row.getByRole('button', { name: 'Trace' })).toBeFocused();
});

test('country with no published geometry or values renders explicit empty/degraded states', async ({ page }) => {
  await page.goto('/country/XXX');
  await expect(page.getByRole('alert')).toContainText('is not one of the V1 country profiles');
  await expect(page.getByRole('region', { name: 'Result context' })).toBeVisible();
  await page.goto('/country/DZA');
  await settle(page);
  // No panel shows a bare zero headline for a missing latest value.
  const headlines = await page.locator('.headline').allInnerTexts();
  for (const h of headlines) expect(h).not.toMatch(/:\s*0(\s|$)/);
});
