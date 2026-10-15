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

  // A reported DZA value that the provider flags as estimated / not directly reported says so.
  const obs = await apiGet(page.request, `/v1/observations?subject_id=DZA&concept=${CRUDE_M}&limit=1000`);
  const flagged = (obs.data as any[]).filter((o) => o.value.kind !== 'missing' && o.source_flags?.provider_estimation_flagged === true);
  for (const o of flagged) {
    const period = String(o.support.period_label ?? o.support.temporal?.label);
    const row = crude.getByRole('row').filter({ has: page.getByRole('rowheader', { name: period, exact: true }) });
    await expect(row.getByTestId('source-flag'), `${period} carries the provider estimation flag`).toContainText('provider estimate');
  }
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
  // Storage location of the bytes is reachable too.
  await expect(drawer.locator('.prov-storage_location').first()).toBeVisible();

  // Following the artifact keeps the SHA-256 (drill-down works), Escape closes and restores focus.
  await raw.first().getByRole('button', { name: /Trace from this artifact/ }).click();
  await expect(drawer.locator('.state-loading')).toHaveCount(0, { timeout: 30_000 });
  await expect(drawer).toContainText(rawSha);
  await drawer.getByRole('button', { name: 'Back' }).click();
  await page.keyboard.press('Escape');
  await expect(drawer).toHaveCount(0);
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
