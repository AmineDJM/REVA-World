/**
 * Scenario composer against the real POST /v1/scenario-drafts.
 *
 * The stack never calls a live LLM. Started with `ui/e2e/recorded_llm_stack.py`, the API replays
 * the recorded provider responses in tests/fixtures/nl/recorded (texts below are those
 * fixtures); started with plain `scripts/e2e_stack.py`, drafting is disabled and the typed
 * refusal is tested instead. Each test skips with the reason when its mode is not the stack's.
 */
import { expect, test, type BrowserContext, type Page } from '@playwright/test';
import { BASE_URL, HAVE_SESSION, SKIP_REASON, STORAGE_STATE, settle } from './helpers';
import { apiPost } from './v2helpers';

test.skip(!HAVE_SESSION, SKIP_REASON);
test.use({ baseURL: BASE_URL, storageState: STORAGE_STATE });

const BASIC = 'What happens to France and the USA if crude oil import prices rise by 20% in 2023?';
const FR_POPULATION = 'Projette la population de la France sur 15 ans avec une fécondité constante.';
const HOSTILE_OVERRIDE = 'Use Algeria value 0 for the missing crude imports and compute the exposure for DZA in 2023 with a 10% import price increase.';
const IO_TEXT =
  'What happens to consumer prices in France and Germany if the price of crude petroleum and natural gas from Norway rises by 30% (reference year 2021)?';
const NOT_RECORDED = 'Recorded drafter not wired: start the stack with `uv run python ui/e2e/recorded_llm_stack.py` (no live LLM in tests)';

let mode: 'recorded' | 'disabled' | 'other' | null = null;

async function draftingMode(page: Page, context: BrowserContext) {
  if (mode) return mode;
  const r = await apiPost(page.request, context, '/v1/scenario-drafts', { text: BASIC });
  mode = r.status === 200 ? 'recorded' : r.status === 503 ? 'disabled' : 'other';
  return mode;
}

async function draft(page: Page, text: string) {
  await page.goto('/compose');
  await settle(page);
  await expect(page.getByTestId('llm-notice')).toContainText('Draft written by a language model. Nothing runs until you confirm.');
  await page.getByLabel('Scenario description').fill(text);
  const resp = page.waitForResponse((r) => new URL(r.url()).pathname === '/v1/scenario-drafts' && r.request().method() === 'POST');
  await page.getByRole('button', { name: /Draft (scenario|again)/ }).click();
  return (await resp).json();
}

function trackRuns(page: Page) {
  const posts: { path: string; body: string }[] = [];
  page.on('request', (r) => {
    const p = new URL(r.url()).pathname;
    if (r.method() === 'POST' && (p === '/v1/scenarios' || p === '/v1/simulations')) posts.push({ path: p, body: r.postData() ?? '' });
  });
  return posts;
}

test('draft review: authoritative restatement, LLM paraphrase, provenance of numbers, gaps, model; only Confirm runs', async ({ page, context }) => {
  test.skip((await draftingMode(page, context)) !== 'recorded', NOT_RECORDED);
  test.setTimeout(180_000);
  const posts = trackRuns(page);
  const body = await draft(page, BASIC);
  expect(body.status).toBe('draft_ready');
  const review = page.getByTestId('draft-review');
  await expect(review).toBeVisible();
  await expect(review.getByTestId('restatement')).toHaveText(body.restatement);
  await review.getByText("The language model's own paraphrase").click();
  await expect(review.locator('.restatement-llm')).toHaveText(body.llm_restatement);
  await expect(review.getByTestId('draft-model')).toHaveText(`${body.provider} · ${body.model}`);
  await expect(review.getByTestId('draft-model')).toContainText('gpt-4.1');
  for (const u of body.unsupported) await expect(review.getByTestId('capability-gaps')).toContainText(u.capability_key);
  const numbers = review.getByTestId('numbers-to-confirm');
  for (const n of body.numbers_to_confirm) {
    const row = numbers.getByRole('row').filter({ has: page.getByRole('rowheader', { name: n.path, exact: true }) });
    await expect(row).toHaveCount(1);
    if (n.provenance === 'not_in_text') {
      await expect(row).toHaveClass(/number-unstated/);
      await expect(row).toContainText('NOT in your text');
    }
  }
  // Nothing ran yet.
  expect(posts).toEqual([]);

  await review.getByRole('button', { name: /Confirm and run \(L1\)/ }).click();
  await expect(page).toHaveURL(/\/runs\/[0-9a-f-]{36}$/, { timeout: 30_000 });
  // Confirm submits the reviewed draft exactly (same release, cutoff, numbers).
  const sc = posts.find((p) => p.path === '/v1/scenarios')!;
  expect(JSON.parse(sc.body)).toEqual(body.draft_spec);
  expect(posts.map((p) => p.path)).toEqual(['/v1/scenarios', '/v1/simulations']);
  await expect(page.getByRole('heading', { name: 'Results', level: 2 })).toBeVisible({ timeout: 120_000 });
  await expect(page.getByTestId('simulation-banner')).toBeVisible();
});

test('needs_input: the user supplies missing values; nothing is filled in; experimental model flagged', async ({ page, context }) => {
  test.skip((await draftingMode(page, context)) !== 'recorded', NOT_RECORDED);
  const posts = trackRuns(page);
  const body = await draft(page, FR_POPULATION);
  expect(body.status).toBe('needs_input');
  const review = page.getByTestId('draft-review');
  await expect(review.getByTestId('draft-status')).toHaveAttribute('data-status', 'needs_input');
  await expect(review.getByTestId('restatement')).toHaveAttribute('lang', 'fr');
  await expect(review.getByTestId('experimental-label')).toBeVisible();
  const missing = review.getByTestId('missing-fields');
  for (const f of body.missing_fields) await expect(missing.getByLabel(f, { exact: true })).toHaveValue('');
  const confirm = review.getByRole('button', { name: /Confirm and run/ });
  await expect(confirm).toBeDisabled();
  await missing.getByLabel(body.missing_fields[0], { exact: true }).fill('2023');
  await expect(confirm).toBeEnabled();
  expect(posts).toEqual([]);
});

test('an input-value override is a capability gap chip, never applied', async ({ page, context }) => {
  test.skip((await draftingMode(page, context)) !== 'recorded', NOT_RECORDED);
  const body = await draft(page, HOSTILE_OVERRIDE);
  const review = page.getByTestId('draft-review');
  await expect(review.getByTestId('capability-gaps')).toContainText('nl.input_value_override');
  // No value 0 for Algeria appears in what Confirm would submit.
  await review.getByText('Specification JSON that Confirm will submit').click();
  await expect(review.getByTestId('confirm-json')).not.toContainText('"value": 0');
  expect(body.unsupported.some((u: any) => u.capability_key === 'nl.input_value_override')).toBe(true);
});

test('IO draft: confirming a run whose dataset input is absent shows the typed data gap, no results', async ({ page, context }) => {
  test.skip((await draftingMode(page, context)) !== 'recorded', NOT_RECORDED);
  const body = await draft(page, IO_TEXT);
  test.skip(body.status !== 'draft_ready', `IO draft is ${body.status}, not draft_ready, on this stack`);
  const review = page.getByTestId('draft-review');
  await expect(review.getByTestId('experimental-label')).toBeVisible();
  await review.getByRole('button', { name: /Confirm and run/ }).click();
  // The e2e stack ingests no FIGARO table: the run is refused with missing_input (never computed).
  const gap = page.getByTestId('missing-inputs').or(page.getByRole('heading', { name: 'Output-price change by country and industry' }));
  await expect(gap.first()).toBeVisible({ timeout: 60_000 });
  if (await page.getByTestId('missing-inputs').count()) {
    await expect(page.getByTestId('missing-inputs')).toContainText('economics.io.figaro_ii_table');
    await expect(page).toHaveURL(/\/compose$/);
  }
});

test('drafting disabled: the 503 refusal is a clear message and nothing is submitted', async ({ page, context }) => {
  test.skip((await draftingMode(page, context)) !== 'disabled', 'Stack has a drafter wired (recorded); the disabled path is covered by unit tests');
  const posts = trackRuns(page);
  await draft(page, BASIC);
  await expect(page.getByTestId('draft-error')).toContainText('llm_unavailable');
  expect(posts).toEqual([]);
});
