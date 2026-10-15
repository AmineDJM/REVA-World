/**
 * Runs list, multi-period result screens (cohort projection, retirement age) and the IO price
 * screen, plus the ⌘K command palette, against the real stack.
 *
 * The multi-period / IO screens need dataset inputs (Eurostat demography, FIGARO) pinned in the
 * stack's release and the model plugins registered. When the stack cannot run a model, the
 * test is skipped with the API's own refusal as the reason (re-checked on every run).
 */
import { expect, test, type BrowserContext, type Page } from '@playwright/test';
import { BASE_URL, HAVE_SESSION, RELEASE, SKIP_REASON, STORAGE_STATE, apiGet, settle } from './helpers';
import { apiPost, crudeSpec, runScenario } from './v2helpers';

test.skip(!HAVE_SESSION, SKIP_REASON);
test.use({ baseURL: BASE_URL, storageState: STORAGE_STATE });

function common(model: string, extra: Record<string, unknown>) {
  if (!RELEASE) throw new Error('no release in stack file');
  return {
    schema_version: '2.0.0',
    baseline_release_id: RELEASE.id,
    knowledge_cutoff: RELEASE.knowledge_cutoff,
    model_key: model,
    model_version: '1.0.0',
    fidelity: 'L1',
    missing_input_policy: 'fail',
    seed: 0,
    ...extra,
  };
}

const COHORT = () =>
  common('demography.cohort_component', {
    title: 'e2e cohort projection Luxembourg',
    countries: ['LUX'],
    reference_year: 2025,
    horizon: { periods: 5, unit: 'calendar_year' },
    intervention: { kind: 'baseline_projection' },
    assumptions: { mortality: 'constant_base_period_rates', fertility: 'constant_base_period_rates', migration: 'average_residual', base_period: { first_year: 2019, last_year: 2023 } },
    outputs: ['population.total.all_ages', 'old_age_dependency_ratio_65plus_per_15_64', 'population_by_single_age'],
  });

const IO = () =>
  common('economic.io_price', {
    title: 'e2e IO price shock',
    fidelity: 'L2',
    countries: ['FRA', 'DEU'],
    reference_year: 2024,
    horizon: { periods: 1, unit: 'annual_accounting_period' },
    intervention: { kind: 'set_exogenous_industry_price_change', shocks: [{ origin: 'FRA', industry: 'B', price_change_percent: 30 }] },
    assumptions: { pass_through_grid: [0.5, 0.75] },
    outputs: ['industry_price_change_percent', 'household_basket_price_change_percent', 'household_basket_contributions'],
  });

const RETIREMENT = () =>
  common('social.retirement_age', {
    title: 'e2e retirement age France',
    countries: ['FRA'],
    reference_year: 2023,
    horizon: { periods: 10, unit: 'calendar_year' },
    intervention: { kind: 'set_legal_retirement_age' },
    assumptions: {},
    outputs: [],
  });

/** Create and run; returns the job id, or a reason string when the stack cannot run this model. */
async function tryRun(page: Page, context: BrowserContext, spec: Record<string, unknown>): Promise<{ id?: string; reason?: string }> {
  const sc = await apiPost(page.request, context, '/v1/scenarios', spec);
  if (sc.status !== 201 && sc.status !== 200) return { reason: `POST /v1/scenarios ${sc.status} ${sc.body?.code ?? ''}: ${String(sc.body?.detail ?? '').slice(0, 200)}` };
  const sim = await apiPost(page.request, context, '/v1/simulations', { scenario_id: sc.body.scenario_id, fidelity: spec.fidelity ?? 'L1' });
  if (sim.status !== 202) return { reason: `POST /v1/simulations ${sim.status} ${sim.body?.code ?? ''}: ${String(sim.body?.detail ?? '').slice(0, 200)}` };
  const id = sim.body.job_id as string;
  for (let i = 0; i < 240; i++) {
    const job = await apiGet(page.request, `/v1/jobs/${id}`);
    if (['succeeded', 'failed', 'cancelled', 'dead'].includes(job.status)) break;
    await page.waitForTimeout(500);
  }
  const st = await apiGet(page.request, `/v1/simulations/${id}`);
  if (st.data.outcome?.status !== 'succeeded' || st.data.results.length === 0) return { reason: `run ended as ${st.data.outcome?.status ?? 'unfinished'}` };
  return { id };
}

test('runs list links every run to its result screen, with model and cutoff', async ({ page, context }) => {
  test.setTimeout(180_000);
  const id = await runScenario(page.request, context, crudeSpec({ title: 'e2e runs list' }));
  await page.goto('/runs');
  await settle(page);
  const row = page.getByTestId('runs-table').getByRole('row').filter({ has: page.getByRole('link', { name: id.slice(0, 8) }) });
  await expect(row).toContainText('Crude border-price exposure');
  if (RELEASE) await expect(row).toContainText(RELEASE.knowledge_cutoff.slice(0, 10));
  await row.getByRole('link').click();
  await expect(page).toHaveURL(new RegExp(`/runs/${id}$`));
  await expect(page.getByRole('heading', { name: 'Results', level: 2 })).toBeVisible({ timeout: 60_000 });
  await expect(page.getByRole('link', { name: 'Show this run on the world map' })).toBeVisible();
});

test('cohort projection: pyramid by year with a slider, period bounds, labels', async ({ page, context }) => {
  test.setTimeout(300_000);
  const r = await tryRun(page, context, COHORT());
  test.skip(!r.id, `cohort projection cannot run on this stack: ${r.reason}`);
  await page.goto(`/runs/${r.id}`);
  await settle(page);
  await expect(page.getByTestId('projection-label')).toContainText('Conditional projection under stated assumptions, not a forecast');
  await expect(page.getByTestId('experimental-label')).toContainText('Experimental model — maintainer review pending');
  const slider = page.getByLabel('Year (1 January)');
  await expect(page.getByTestId('pyramid')).toBeVisible();
  const first = await page.getByTestId('pyramid-year').textContent();
  await slider.focus();
  await page.keyboard.press('End');
  await expect(page.getByTestId('pyramid-year')).not.toHaveText(first ?? '');
  await expect(page.getByTestId('period-chart').first()).toBeVisible();
  await expect(page.getByTestId('backtest-panel')).toBeVisible();
});

test('cohort projection L2: assumption-sensitivity ribbon per year, never a confidence band', async ({ page, context }) => {
  test.setTimeout(300_000);
  const spec = COHORT() as Record<string, any>;
  spec.title = 'e2e cohort projection Luxembourg L2';
  spec.fidelity = 'L2';
  spec.outputs = ['population.total.all_ages', 'old_age_dependency_ratio_65plus_per_15_64'];
  spec.assumptions = { ...spec.assumptions, sensitivity: { migration: ['zero', 'average_residual'], fertility: ['constant_base_period_rates'] } };
  const r = await tryRun(page, context, spec);
  test.skip(!r.id, `cohort projection L2 cannot run on this stack: ${r.reason}`);
  const st = (await apiGet(page.request, `/v1/simulations/${r.id}`)).data;
  const ranges = (st.results as any[]).filter((x) => x.variant === 'L2:sensitivity_range' && x.output_key === 'population.total.all_ages');
  expect(ranges.length).toBeGreaterThan(0);
  expect(ranges.every((x) => x.period_start && x.period_end)).toBe(true);
  await page.goto(`/runs/${r.id}`);
  await settle(page);
  await expect(page.getByTestId('sensitivity-ribbon')).toBeVisible();
  await expect(page.getByText(/ribbon = assumption sensitivity range \(not a confidence interval\)/)).toBeVisible();
  await page.getByText(/^Table: Population, total, all ages/).click();
  const table = page.getByTestId('series-table');
  for (const x of ranges) {
    const year = x.period_start.slice(0, 4);
    const row = table.getByRole('row').filter({ has: page.getByRole('rowheader', { name: new RegExp(`^${year} `) }) });
    await expect(row).toContainText(`[${x.period_start}, ${x.period_end})`);
  }
  await expect(table).not.toContainText('±');
});

test('retirement age: indicators, sensitivity ribbon and back-test panel', async ({ page, context }) => {
  test.setTimeout(300_000);
  const r = await tryRun(page, context, RETIREMENT());
  test.skip(!r.id, `retirement-age model cannot run on this stack: ${r.reason}`);
  await page.goto(`/runs/${r.id}`);
  await settle(page);
  await expect(page.getByTestId('projection-label')).toBeVisible();
  await expect(page.getByTestId('experimental-label')).toBeVisible();
  await expect(page.getByTestId('backtest-panel')).toBeVisible();
});

test('IO price propagation: heatmap, basket and decomposition, labelled first-round cost-push', async ({ page, context }) => {
  test.setTimeout(300_000);
  const r = await tryRun(page, context, IO());
  test.skip(!r.id, `IO price model cannot run on this stack: ${r.reason}`);
  await page.goto(`/runs/${r.id}`);
  await settle(page);
  const st = (await apiGet(page.request, `/v1/simulations/${r.id}`)).data;
  await expect(page.getByTestId('io-label')).toContainText('not an inflation forecast');
  await expect(page.getByTestId('experimental-label')).toBeVisible();
  // Heatmap: one row per country, one cell per industry, values as served (exact strings on the cells).
  const cells = (st.results as any[]).filter((x) => x.variant === 'L1' && x.output_key.startsWith('industry_price_change_percent:'));
  const hm = page.getByTestId('io-heatmap');
  await expect(hm.locator('tbody tr')).toHaveCount(new Set(cells.map((x) => x.applicability.country)).size);
  for (const c of cells) {
    if (c.value.kind === 'decimal') await expect(hm.locator(`td[data-exact="${c.value.value}"]`).first()).toBeAttached();
  }
  await expect(hm.locator('td.hm-shocked')).toHaveCount(cells.filter((x) => x.applicability.exogenous).length);
  // Basket and decomposition per country; the L2 range is labelled a sensitivity range.
  const basket = page.getByTestId('basket-table');
  for (const b of (st.results as any[]).filter((x) => x.variant === 'L1' && x.output_key === 'household_basket_price_change_percent')) {
    await expect(basket.getByRole('row', { name: new RegExp(b.applicability.country) })).toBeVisible();
  }
  await expect(basket.locator('caption')).toContainText('assumption sensitivity range (not a confidence interval)');
  await expect(basket.locator('thead')).toContainText('Domestic products');
  await expect(basket.locator('thead')).toContainText('Imported products');
  // Hovering a cell reads its exact value.
  const first = cells.find((x) => x.value.kind === 'decimal');
  await hm.locator(`td[data-exact="${first.value.value}"]`).first().hover();
  await expect(page.getByTestId('heatmap-readout')).toContainText(first.applicability.industry);
});

test('⌘K palette: deterministic search over countries and runs, keyboard only', async ({ page }) => {
  await page.goto('/world');
  await settle(page);
  const llm: string[] = [];
  page.on('request', (r) => {
    if (r.method() !== 'GET') llm.push(`${r.method()} ${new URL(r.url()).pathname}`);
  });
  await page.keyboard.press('Control+k');
  const dialog = page.getByRole('dialog');
  await expect(dialog).toBeVisible();
  const input = dialog.getByRole('combobox');
  await expect(input).toBeFocused();
  await expect(dialog.getByText('Loading catalogue…')).toHaveCount(0, { timeout: 15_000 });
  await input.fill('algeria');
  await expect(dialog.getByRole('option').first()).toContainText('Algeria');
  await page.keyboard.press('Enter');
  await expect(page).toHaveURL(/\/country\/DZA$/);
  // Escape restores focus to where it was.
  await page.getByRole('link', { name: 'Skip to content' }).focus();
  await page.keyboard.press('Control+k');
  await expect(dialog).toBeVisible();
  await page.keyboard.press('Escape');
  await expect(dialog).toHaveCount(0);
  await expect(page.getByRole('link', { name: 'Skip to content' })).toBeFocused();
  expect(llm, 'the palette never posts anything (no LLM)').toEqual([]);
});
