/**
 * Simulation screen end to end against the real API and a real worker
 * (CI item 10: scenario banner; §13 every simulation screen and export has scenario
 * context; negative/degraded states render clearly; no stub success).
 */
import { readFileSync } from 'node:fs';
import { expect, test, type Page } from '@playwright/test';
import { BASE_URL, FIXTURE_BASELINE_UUID, HAVE_SESSION, RELEASE, SKIP_REASON, STORAGE_STATE, apiGet, csrfToken, settle } from './helpers';

test.skip(!HAVE_SESSION, SKIP_REASON);
test.use({ baseURL: BASE_URL, storageState: STORAGE_STATE });

const RUN_TIMEOUT = 180_000;

async function openForm(page: Page) {
  await page.goto('/simulation');
  await expect(page.getByTestId('simulation-screen-banner')).toContainText('SIMULATION');
  await settle(page);
  await expect(page.getByLabel('Pinned belief release')).toBeVisible();
  if (RELEASE) await expect(page.getByLabel('Pinned belief release')).toHaveValue(RELEASE.id);
}

function checkbox(page: Page, label: string) {
  return page.getByRole('group', { name: 'Countries' }).getByRole('checkbox', { name: label, exact: true });
}

/** Submit the form and wait for the job to finish; returns the simulation (job) id. */
async function runAndWait(page: Page): Promise<string> {
  const posted: string[] = [];
  page.on('request', (r) => {
    if (r.method() === 'POST' && /\/v1\/(scenarios|simulations)$/.test(new URL(r.url()).pathname)) posted.push(r.postData() ?? '');
  });
  await page.getByRole('button', { name: 'Create scenario and run' }).click();
  await expect(page).toHaveURL(/[?&]sim=[0-9a-f-]{36}/, { timeout: 30_000 });
  const simId = new URL(page.url()).searchParams.get('sim')!;
  const results = page.locator('section.panel').filter({ has: page.getByRole('heading', { name: 'Results', level: 2 }) });
  const runStatus = page.locator('section.panel').filter({ has: page.getByRole('heading', { name: 'Run status', level: 2 }) });
  const failure = runStatus.getByRole('alert');
  // Fail fast on a degraded state (job error / status endpoint error) instead of timing out.
  await expect(results.or(failure).first()).toBeVisible({ timeout: RUN_TIMEOUT });
  if (await failure.count()) {
    throw new Error(`simulation ${simId} did not load results: ${(await failure.first().innerText()).replace(/\s+/g, ' ')}`);
  }
  await expect(results, 'simulation finished and results loaded').toBeVisible();
  for (const body of posted) expect(body).not.toContain(FIXTURE_BASELINE_UUID);
  return simId;
}

async function download(page: Page, button: string): Promise<string> {
  const [dl] = await Promise.all([page.waitForEvent('download'), page.getByRole('button', { name: button }).click()]);
  const path = await dl.path();
  return readFileSync(path, 'utf-8');
}

test('L1 FRA/CHN/USA run completes with results, persistent SIMULATION banner and full-context exports', async ({ page }) => {
  test.setTimeout(RUN_TIMEOUT + 60_000);
  await openForm(page);
  for (const c of ['FRA', 'CHN', 'USA']) await expect(checkbox(page, c)).toBeChecked();
  await expect(checkbox(page, 'DZA')).not.toBeChecked();
  await page.getByRole('radio', { name: /L1/ }).check();
  const simId = await runAndWait(page);

  // Ground truth from the API: the run really succeeded and produced results.
  const sim = await apiGet(page.request, `/v1/simulations/${simId}`);
  expect(sim.data.outcome?.status).toBe('succeeded');
  expect(sim.data.results.length).toBeGreaterThanOrEqual(12); // 3 countries x 4 outputs
  expect(sim.modality).toBe('assumption_conditioned_scenario');
  const scenarioId: string = sim.data.scenario.id;
  const releaseId: string = sim.data.scenario.baseline_release.id;
  if (RELEASE) expect(releaseId).toBe(RELEASE.id);

  // L1 identity check on the served numbers: dM = (p_M - 1) * M with p_M = 1.25.
  const fraImport = (sim.data.results as any[]).find(
    (r) => r.output_key === 'import_value_change_usd' && r.variant === 'L1' && /France/.test(r.subject?.label ?? ''),
  );
  expect(fraImport, 'France import change result').toBeDefined();
  const M = Number(fraImport.applicability.inputs.M.value);
  expect(M).toBeGreaterThan(0);
  expect(Number(fraImport.value.value)).toBeCloseTo(0.25 * M, 0);

  // UI: outcome, scenario context and results for each country.
  const results = page.locator('section.panel').filter({ has: page.getByRole('heading', { name: 'Results', level: 2 }) });
  await expect(results).toContainText('outcome succeeded');
  await expect(results).toContainText(scenarioId.slice(0, 8));
  const table = page.getByTestId('comparison-table');
  for (const country of ['France', 'China', 'United States']) {
    await expect(table.getByRole('rowheader', { name: new RegExp(country) }).first()).toBeVisible();
  }
  await expect(table.locator('.missing-value')).toHaveCount(0);
  const banner = page.getByRole('region', { name: 'Result context' }).first();
  await expect(banner.getByTestId('simulation-banner')).toContainText('SIMULATION');
  await expect(banner).toContainText('Assumption-conditioned scenario');
  await expect(banner).toContainText(releaseId.slice(0, 8));
  await expect(banner).toContainText('Knowledge cutoff');
  await expect(banner.getByTestId('fixture-label')).toContainText('TEST FIXTURE');

  // The screen banner stays visible while scrolling through results.
  await page.evaluate(() => window.scrollTo(0, document.body.scrollHeight));
  await expect(page.getByTestId('simulation-screen-banner')).toBeInViewport();
  await expect(page.getByTestId('simulation-screen-banner')).toContainText('SIMULATION');

  // CSV export carries the full scenario context.
  const csv = await download(page, 'Export CSV (with scenario context)');
  expect(csv.split('\n')[0]).toMatch(/^# SIMULATION/);
  expect(csv).toContain(`# simulation_id: ${simId}`);
  expect(csv).toContain(`# scenario_id: ${scenarioId}`);
  expect(csv).toContain(`# baseline_release_id: ${releaseId}`);
  expect(csv).toMatch(/# knowledge_cutoff: \d{4}-\d{2}-\d{2}T/);
  expect(csv).toMatch(/# model_versions: .*economic\.crude_exposure@1\.0\.0/);
  expect(csv).toContain('# fidelity: L1');
  expect(csv).toMatch(/# assumption: .*Intervention/);
  expect(csv).toMatch(/# assumption: .*No general equilibrium/);
  expect(csv).not.toMatch(/: unknown$/m);
  const header = csv.split('\n').find((l) => !l.startsWith('#'));
  expect(header).toBe('country,output_key,variant,value,unit_ref,missing_reason,uncertainty,lower,upper');
  expect(csv.split('\n').filter((l) => l && !l.startsWith('#')).length - 1).toBe(sim.data.results.length);

  // JSON export carries the same context and the served values.
  const json = JSON.parse(await download(page, 'Export JSON (with scenario context)'));
  expect(json.banner).toMatch(/^SIMULATION/);
  expect(json.modality).toBe('assumption_conditioned_scenario');
  expect(json.context.simulation_id).toBe(simId);
  expect(json.context.scenario_id).toBe(scenarioId);
  expect(json.context.baseline_release_id).toBe(releaseId);
  expect(json.context.knowledge_cutoff).toBeTruthy();
  expect(json.context.model_versions).toContain('economic.crude_exposure@1.0.0');
  expect(json.context.fidelity).toBe('L1');
  expect(json.context.assumptions.length).toBeGreaterThanOrEqual(3);
  expect(json.context.origin).toBe('test_fixture');
  const exported = (json.results as any[]).find((r) => r.result_id === fraImport.id);
  expect(exported?.value).toEqual(fraImport.value);
});

test('L2 shows the sensitivity range labelled "not a confidence interval"', async ({ page }) => {
  test.setTimeout(RUN_TIMEOUT + 60_000);
  await openForm(page);
  await page.getByRole('radio', { name: /L2/ }).check();
  await expect(page.getByText(/sensitivity range, not a\s+confidence interval/)).toBeVisible();
  const simId = await runAndWait(page);

  const sim = await apiGet(page.request, `/v1/simulations/${simId}`);
  expect(sim.data.outcome?.status).toBe('succeeded');
  expect(sim.data.run.fidelity).toBe('L2');
  expect((sim.data.results as any[]).some((r) => r.uncertainty?.kind === 'sensitivity_range')).toBe(true);
  // No served L2 result claims a probability-bearing interval.
  for (const r of sim.data.results as any[]) {
    expect(['sensitivity_range', 'none', 'not_quantified', 'exact', undefined]).toContain(r.uncertainty?.kind);
  }

  const table = page.getByTestId('comparison-table');
  await expect(table.locator('caption')).toContainText('sensitivity range (not a confidence interval)');
  await expect(table.getByRole('columnheader', { name: 'Sensitivity range (not a confidence interval)' })).toBeVisible();
  await expect(table.locator('.sensitivity-range').first()).toBeVisible();
  // Nothing on the page presents the band as a confidence/credible interval or a 95% band.
  const text = await page.locator('main').innerText();
  expect(text.replace(/not a confidence interval/gi, '')).not.toMatch(/confidence interval|credible interval|\b95\s?%/i);
  await page.evaluate(() => window.scrollTo(0, document.body.scrollHeight));
  await expect(page.getByTestId('simulation-screen-banner')).toBeInViewport();

  const json = JSON.parse(await download(page, 'Export JSON (with scenario context)'));
  expect(json.context.fidelity).toBe('L2');
  expect(json.context.scenario_id).toBe(sim.data.scenario.id);
  expect(json.context.assumptions.join(' ')).toMatch(/no probability interpretation/);
  const ranged = (json.results as any[]).find((r) => r.uncertainty?.kind === 'sensitivity_range');
  expect(ranged.uncertainty.label).not.toMatch(/confidence/i);
});

test('a run including DZA is rejected with missing_input naming DZA (never imputed, no results)', async ({ page }) => {
  await openForm(page);
  await checkbox(page, 'DZA').check();
  await expect(page.getByRole('note')).toContainText('missing_input');
  await page.getByRole('radio', { name: /L1/ }).check();
  const responses: { url: string; status: number }[] = [];
  page.on('response', (r) => {
    if (r.request().method() === 'POST') responses.push({ url: new URL(r.url()).pathname, status: r.status() });
  });
  await page.getByRole('button', { name: 'Create scenario and run' }).click();
  const problem = page.locator('form .problem[role="alert"]');
  await expect(problem).toBeVisible({ timeout: 30_000 });
  await expect(problem).toContainText('missing_input');
  await expect(problem).toContainText('HTTP 422');
  await expect(problem).toContainText('"country": "DZA"');
  await expect(problem).not.toContainText('"country": "FRA"');
  // The run was refused before any job existed: no sim id, no results, no fabricated output.
  expect(responses.find((r) => r.url === '/v1/simulations')?.status).toBe(422);
  expect(new URL(page.url()).searchParams.get('sim')).toBeNull();
  await expect(page.getByRole('heading', { name: 'Results', level: 2 })).toHaveCount(0);
  await expect(page.getByTestId('simulation-screen-banner')).toBeVisible();
});

test('L3 is not offered in the UI and is rejected by the API with a typed 422', async ({ page, context }) => {
  await openForm(page);
  const fidelity = page.getByRole('group', { name: 'Fidelity' });
  await expect(fidelity.getByRole('radio')).toHaveCount(2);
  await expect(fidelity).not.toContainText('L3');

  const token = await csrfToken(context);
  const scenarios = await apiGet(page.request, '/v1/scenarios?limit=1');
  const scenarioId = scenarios.data[0]?.id ?? '7b0e6f0a-0000-4000-8000-000000000000';
  const r = await page.request.post('/v1/simulations', {
    data: { scenario_id: scenarioId, fidelity: 'L3' },
    headers: { 'X-CSRF-Token': token, 'Idempotency-Key': crypto.randomUUID() },
  });
  expect(r.status()).toBe(422);
  const p = await r.json();
  expect(r.headers()['content-type']).toContain('application/problem+json');
  expect(JSON.stringify(p)).toMatch(/L3/);
  expect(p).not.toHaveProperty('job_id');

  // A scenario spec declaring L3 is refused as well.
  if (scenarios.data[0]) {
    const spec = { ...scenarios.data[0].spec, fidelity: 'L3', title: 'L3 attempt' };
    const s = await page.request.post('/v1/scenarios', { data: spec, headers: { 'X-CSRF-Token': token } });
    expect(s.status()).toBe(422);
  }
});

test('simulation screen degrades clearly for an unknown run id', async ({ page }) => {
  await page.goto('/simulation?sim=7b0e6f0a-0000-4000-8000-000000000000');
  await expect(page.getByTestId('simulation-screen-banner')).toContainText('SIMULATION');
  const status = page.locator('section.panel').filter({ has: page.getByRole('heading', { name: 'Run status' }) });
  await expect(status.getByRole('alert')).toContainText(/Not found/, { timeout: 30_000 });
  await expect(page.getByRole('heading', { name: 'Results', level: 2 })).toHaveCount(0);
});
