/**
 * V2 world map and mirror view against the real stack: a crude run with
 * missing_input_policy=skip_missing_countries (FRA, DZA, CHN, USA), its choropleth, the hatched
 * country with its typed reason on hover, reported partner arcs, and the mirror view of DZA.
 */
import { expect, test, type Page } from '@playwright/test';
import { BASE_URL, HAVE_SESSION, SKIP_REASON, STORAGE_STATE, apiGet, settle } from './helpers';
import { crudeSpec, runScenario } from './v2helpers';

test.skip(!HAVE_SESSION, SKIP_REASON);
test.use({ baseURL: BASE_URL, storageState: STORAGE_STATE });
test.describe.configure({ mode: 'serial' });

let simId = '';

async function ensureRun(page: Page, context: import('@playwright/test').BrowserContext) {
  if (simId) return simId;
  simId = await runScenario(
    page.request,
    context,
    crudeSpec({ schema_version: '2.0.0', title: 'e2e map: skip missing countries', countries: ['FRA', 'DZA', 'CHN', 'USA'], missing_input_policy: 'skip_missing_countries' }),
  );
  return simId;
}

/** Move the pointer over the map until the tooltip names `name` (the map has no DOM per country). */
async function hoverUntil(page: Page, name: string): Promise<string | null> {
  const map = page.getByTestId('exposure-map');
  await map.scrollIntoViewIfNeeded();
  await page.waitForTimeout(500);
  const box = await map.boundingBox();
  if (!box) return null;
  const tip = page.getByTestId('map-tip');
  for (let y = box.y + box.height * 0.2; y < box.y + box.height * 0.7; y += 10) {
    for (let x = box.x + box.width * 0.3; x < box.x + box.width * 0.62; x += 10) {
      await page.mouse.move(x, y);
      if (await tip.count()) {
        const t = (await tip.textContent()) ?? '';
        if (t.includes(name)) return t;
      }
    }
  }
  return null;
}

test('choropleth comes from the run results; the skipped country is hatched with its typed reason, never zero', async ({ page, context }) => {
  test.setTimeout(180_000);
  const id = await ensureRun(page, context);
  const sim = await apiGet(page.request, `/v1/simulations/${id}`);
  expect(sim.data.outcome.status).toBe('succeeded');
  const exposure = (sim.data.results as any[]).filter((r) => r.output_key === 'exposure_percent_baseline_gdp' && r.variant === 'L1');
  expect(exposure.map((r) => r.applicability.country).sort()).toEqual(['CHN', 'FRA', 'USA']);
  expect(sim.data.outcome.applicability.countries.DZA.status).not.toBe('ready');

  await page.goto(`/map?sim=${id}`);
  await settle(page);
  await expect(page.getByTestId('simulation-banner')).toBeVisible();
  const table = page.getByTestId('run-countries');
  await expect(table).toBeVisible();
  for (const r of exposure) {
    // The exact served string is carried on the cell; the display is rounded.
    await expect(table.locator(`[data-exact="${r.value.value}"]`)).toHaveCount(1);
  }
  const dza = table.locator('tr[data-gap-country="DZA"]');
  await expect(dza).toContainText('Algeria');
  await expect(dza).toContainText('missing: unavailable');
  await expect(dza.locator('.missing-value')).toBeVisible();
  await expect(dza).not.toContainText(/(^|\s)[+−-]?0(\.0+)? %/);

  // The map itself renders (canvas) and the hatched country explains itself on hover.
  await expect(page.getByTestId('exposure-map').locator('canvas')).toBeVisible();
  const tip = await hoverUntil(page, 'Algeria');
  expect(tip, 'hovering Algeria shows its typed reason').not.toBeNull();
  expect(tip!).toMatch(/Algeria: skipped: inputs incomplete — .*missing: unavailable.*Not zero\./);
});

test('partner arcs: only reported flows; World never an arc; composite partners listed, not mapped', async ({ page, context }) => {
  test.setTimeout(180_000);
  const id = await ensureRun(page, context);
  const fra = await apiGet(page.request, '/v1/entities/FRA');
  await page.goto(`/map?sim=${id}&focus=${fra.data.id}&flow=M`);
  await settle(page);
  const sim = await apiGet(page.request, `/v1/simulations/${id}`);
  const cutoff = encodeURIComponent(sim.data.scenario.knowledge_cutoff);
  const rels = await apiGet(page.request, `/v1/relations?entity_id=FRA&layer=trade.crude_petroleum&period=2023&as_of=${cutoff}&max_edges=500`);
  const imports = (rels.data.relations as any[]).filter(
    (r) => r.component.flow === 'M' && r.participants.find((p: any) => p.role === 'role.reporter')?.entity_id === fra.data.id,
  );
  const partnerOf = (r: any) => r.participants.find((p: any) => p.role === 'role.partner');
  const countries = imports.filter((r) => partnerOf(r).entity_kind === 'entity.country' && partnerOf(r).entity_id !== fra.data.id);
  const composites = imports.filter((r) => partnerOf(r).entity_kind !== 'entity.country' && !/^world$/i.test(partnerOf(r).entity_label));

  const table = page.getByTestId('partner-table');
  await expect(table.locator('caption')).toContainText(`${countries.length} partner countr`);
  const showAll = page.getByRole('button', { name: /Show all \d+ partners/ });
  if (await showAll.count()) await showAll.click();
  await expect(table.locator('tbody tr')).toHaveCount(countries.length);
  await expect(table.getByRole('rowheader', { name: 'World', exact: true })).toHaveCount(0);
  const arcs = Number(/(\d+) drawn as arcs/.exec((await table.locator('caption').textContent()) ?? '')?.[1]);
  await expect(table.locator('tbody td', { hasText: /^arc$/ })).toHaveCount(arcs);
  // Only partners with reviewed display geometry can be drawn; the rest are listed.
  expect(arcs).toBeLessThanOrEqual(countries.length);
  if (composites.length) {
    const box = page.getByTestId('composite-partners');
    for (const c of composites) await expect(box).toContainText(partnerOf(c).entity_label);
    for (const c of composites) await expect(table.getByRole('rowheader', { name: partnerOf(c).entity_label, exact: true })).toHaveCount(0);
  }
});

test('mirror view: what partners report about Algeria, labelled "reported by partner", never filling Algeria', async ({ page }) => {
  const dza = await apiGet(page.request, '/v1/entities/DZA');
  const rels = await apiGet(page.request, '/v1/relations?entity_id=DZA&layer=trade.crude_petroleum&max_edges=500');
  const mirror = (rels.data.relations as any[]).filter((r) => {
    const rep = r.participants.find((p: any) => p.role === 'role.reporter');
    const par = r.participants.find((p: any) => p.role === 'role.partner');
    return par?.entity_id === dza.data.id && rep?.entity_id !== dza.data.id;
  });
  expect(mirror.length, 'partners report crude flows with Algeria in the fixtures').toBeGreaterThan(0);

  await page.goto('/country/DZA');
  await settle(page);
  const panel = page.getByTestId('mirror-panel');
  await expect(panel).toContainText('reported by partner');
  const rows = panel.getByTestId('mirror-table').locator('tbody tr');
  await expect(rows).toHaveCount(mirror.length);
  for (let i = 0; i < mirror.length; i++) await expect(rows.nth(i)).toContainText('reported by partner');
  for (const r of mirror) {
    if (r.value?.kind === 'decimal') await expect(panel.locator(`[data-exact="${r.value.value}"]`).first()).toBeVisible();
  }
  // Algeria's own crude-import cells stay missing: mirror figures never fill them.
  const crude = page.locator('section.panel').filter({ has: page.getByRole('heading', { name: 'Crude petroleum imports (HS 2709)', level: 2 }) });
  await expect(crude.locator('.missing-value').first()).toBeVisible();
  for (const r of mirror) {
    if (r.value?.kind === 'decimal') await expect(crude.locator(`[data-exact="${r.value.value}"]`)).toHaveCount(0);
  }
});

test('map at 375 px: no horizontal page scroll with a run loaded', async ({ page, context }) => {
  test.setTimeout(180_000);
  const id = await ensureRun(page, context);
  await page.setViewportSize({ width: 375, height: 812 });
  await page.goto(`/map?sim=${id}`);
  await settle(page);
  await expect(page.getByTestId('run-countries')).toBeVisible();
  const [sw, cw] = await page.evaluate(() => [document.documentElement.scrollWidth, document.documentElement.clientWidth]);
  expect(sw).toBeLessThanOrEqual(cw);
});
