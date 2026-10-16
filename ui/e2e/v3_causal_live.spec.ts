/**
 * "Causes & conséquences" against the REAL causal routes of the e2e stack (no route interception):
 * /v1/causal/* answer from the disposable database where `scripts/e2e_stack.py --v3-fixtures` loaded CAUSCORE's
 * real-Wikidata causal fixture (First World War, Great Depression, 1918 flu; origin test_fixture) and turned it
 * into graded claims with reva.causal.refresh. /v1/ask runs the keyword planner over the same claims.
 *
 *   uv run python scripts/e2e_stack.py --v3-fixtures --run -- e2e/v3_causal_live.spec.ts
 *
 * Expectations are cross-checked with the same API the UI reads (never hard-coded counts or grades).
 */
import { existsSync, readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { expect, test } from '@playwright/test';
import { BASE_URL, HAVE_SESSION, SKIP_REASON, STORAGE_STATE, apiGet } from './helpers';

function stackV3(): Record<string, any> | null {
  const path = process.env.REVA_E2E_STACK_FILE ?? fileURLToPath(new URL('../../var/e2e/stack.json', import.meta.url));
  if (!existsSync(path)) return null;
  try {
    return (JSON.parse(readFileSync(path, 'utf-8')) as { v3?: Record<string, any> }).v3 ?? null;
  } catch {
    return null;
  }
}

const V3 = stackV3();
test.skip(!HAVE_SESSION, SKIP_REASON);
test.skip(HAVE_SESSION && !V3?.causal, 'Stack not started with --v3-fixtures (no v3.causal section)');
test.use({ baseURL: BASE_URL, storageState: STORAGE_STATE, actionTimeout: 30_000, navigationTimeout: 60_000 });
test.describe.configure({ timeout: 180_000 });
const SLOW = { timeout: 60_000 };

type Claim = { claim_key: string; stance: string; cause: { id: string }; effect: { id: string }; source: { source_key: string; tier: string; statement_id: string | null } };

test('stack: the causal fixture is loaded and graded', async () => {
  expect(V3!.causal.status, JSON.stringify(V3!.causal)).toBe('completed');
  expect(V3!.causal.loaded.inserted).toBeGreaterThan(10);
  expect(V3!.causal.graded.claims).toBeGreaterThan(10);
});

test('Première Guerre mondiale (Q361): real claims on the graph, every line attributed, an edge opens its Wikidata statement', async ({ page }) => {
  const causes = (await apiGet(page.request, '/v1/causal/events/Q361/causes?limit=200')).data.claims as Claim[];
  const effects = (await apiGet(page.request, '/v1/causal/events/Q361/effects?limit=200')).data.claims as Claim[];
  const edges = new Set([...causes, ...effects].filter((c) => c.stance === 'asserts').map((c) => c.claim_key));
  expect(edges.size).toBeGreaterThan(5);
  await page.goto('/v3/causes?event=Q361');
  await expect(page.getByTestId('event-card').getByRole('heading', { name: 'Première Guerre mondiale' })).toBeVisible(SLOW);
  const canvas = page.getByTestId('causal-canvas');
  await expect(canvas).toHaveAttribute('data-edges', String(edges.size), SLOW);
  for (const tag of await page.getByTestId('causes-list').getByTestId('selon').allTextContents()) expect(tag).toMatch(/^selon Wikidata \(T[34]\)$/);

  const pick = [...causes, ...effects].find((c) => c.stance === 'asserts' && c.source.statement_id)!;
  const pt = await canvas.evaluate((el, id) => {
    const cy = (el as HTMLDivElement & { __cy?: { $id: (i: string) => { renderedMidpoint: () => { x: number; y: number } } } }).__cy!;
    return cy.$id(id).renderedMidpoint();
  }, pick.claim_key);
  const box = (await canvas.boundingBox())!;
  await page.mouse.click(box.x + pt.x, box.y + pt.y);
  const detail = page.getByTestId('claim-detail');
  await expect(detail.getByTestId('claim-statement')).toContainText(pick.source.statement_id!, SLOW);
  await expect(detail.getByTestId('claim-attribution')).toContainText(`selon Wikidata (${pick.source.tier})`);
  await expect(detail.getByTestId('tier-badge')).toHaveText(pick.source.tier);
});

test('"Pourquoi ?" lanes and the timeline come from the same claims', async ({ page }) => {
  const why = (await apiGet(page.request, '/v1/causal/events/Q361/why')).data as { lanes: { lane: string; claims: Claim[] }[] };
  await page.goto('/v3/causes?event=Q361');
  const panel = page.getByTestId('why-panel');
  await expect(panel).toBeVisible(SLOW);
  for (const lane of why.lanes) {
    const n = lane.claims.length;
    await expect(panel.getByTestId(`why-${lane.lane}`).getByTestId('claim-line')).toHaveCount(n);
  }
  await expect(page.getByTestId('causal-timeline')).toBeVisible();
});

test('chain finder: Sarajevo → révolution russe as the chain API composes it (or "no chain", said as such)', async ({ page }) => {
  const api = (await apiGet(page.request, '/v1/causal/chain?from=Q192050&to=Q8729')).data as { found: boolean; paths: { hops: unknown[]; chain_grade: string }[] };
  await page.goto('/v3/causes?event=Q361&de=Q192050&vers=Q8729');
  const finder = page.getByTestId('chain-finder');
  await expect(finder.getByTestId('chain-banner')).toContainText('force = maillon le plus faible');
  if (!api.found) {
    await expect(page.getByTestId('chain-none')).toBeVisible(SLOW);
    return;
  }
  const chains = finder.getByTestId('causal-chain');
  await expect(chains).toHaveCount(api.paths.length, SLOW);
  await expect(chains.first().getByTestId('chain-hop')).toHaveCount(api.paths[0]!.hops.length);
  await expect(chains.first().getByTestId('grade-badge').first()).toHaveAttribute('data-grade', api.paths[0]!.chain_grade);
});

test('candidate space is off by default and separate when on', async ({ page }) => {
  const cand = (await apiGet(page.request, '/v1/causal/candidates?event=Q361')).data.candidates as unknown[];
  await page.goto('/v3/causes?event=Q361');
  const toggle = page.getByTestId('candidates-toggle');
  await expect(toggle).not.toBeChecked(SLOW);
  await toggle.check();
  const panel = page.getByTestId('candidates-panel');
  await expect(panel).toContainText('candidats extraits de textes — non sourcés, non vérifiés');
  if (cand.length === 0) await expect(panel).toContainText('Aucun candidat extrait pour cet événement.', SLOW);
  else await expect(panel.getByTestId('candidate')).toHaveCount(cand.length, SLOW);
});

test('Demander (keyword planner): every causal sentence names its source and tier', async ({ page }) => {
  await page.goto('/v3/demander');
  await page.getByLabel('Votre question').fill('Pourquoi la Première Guerre mondiale a-t-elle éclaté ?');
  await page.getByRole('button', { name: 'Demander' }).click();
  const causal = page.locator('[data-testid="answer-sentence"][data-kind="causal"]');
  await expect(causal.first()).toBeVisible(SLOW);
  for (const s of await causal.all()) await expect(s).toHaveText(/^Selon Wikidata \(T[34], /);
});
