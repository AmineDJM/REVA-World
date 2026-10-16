/**
 * V3 "Planète" screens against the REAL V3 routes of the e2e stack (no route interception):
 * /v1/kg, /v1/links, /v1/atlas, /v1/ask and /v1/locations answer from the disposable database that
 * `scripts/e2e_stack.py --v3-fixtures` loads (the real-Wikidata links fixture through load_batch,
 * atlas sources replayed in fixture mode, a test_fixture atlas snapshot; keyword planner for /v1/ask).
 *
 * Runs only on a stack started with --v3-fixtures (the stack file carries a `v3` section); skipped
 * otherwise with the reason. UI3's intercepted e2e/v3_planet.spec.ts stays for standalone runs.
 *
 *   uv run python scripts/e2e_stack.py --v3-fixtures --run -- e2e/v3_live_stack.spec.ts
 *
 * Expectations are cross-checked with the same API the UI reads (never hard-coded numbers), and every
 * wait is generous: the machine running the suite is often heavily loaded.
 */
import { existsSync, readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { expect, test, type Page } from '@playwright/test';
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
const NOT_V3 = 'Stack not started with --v3-fixtures: run `uv run python scripts/e2e_stack.py --v3-fixtures --run -- e2e/v3_live_stack.spec.ts`';

test.skip(!HAVE_SESSION, SKIP_REASON);
test.skip(HAVE_SESSION && !V3, NOT_V3);
test.use({ baseURL: BASE_URL, storageState: STORAGE_STATE, actionTimeout: 30_000, navigationTimeout: 60_000 });
test.describe.configure({ timeout: 180_000 });

const SLOW = { timeout: 60_000 };
const COMMUNITY = 'communautaire, non vérifié';
/** Atlas concept with fixture data (UNDP global MPI 2026, table 1): values for ~100 countries, the rest are gaps. */
const ATLAS_CONCEPT = 'quality_of_life.mpi_headcount_thousands_2024';

/** "Graphe de N entités et M déclarations …" -> [N, M]. */
async function graphCounts(page: Page): Promise<[number, number]> {
  const label = (await page.getByTestId('neuron-canvas').getAttribute('aria-label')) ?? '';
  const m = /Graphe de (\d+) entités et (\d+) déclarations/.exec(label);
  return m ? [Number(m[1]), Number(m[2])] : [0, 0];
}

test('stack: the V3 fixtures are loaded (KG fixture without rejections, atlas sources, test_fixture snapshot)', async () => {
  expect(V3!.kg.statements).toBeGreaterThan(100);
  expect(V3!.kg.rejected).toBe(0);
  const ok = Object.entries(V3!.atlas as Record<string, any>).filter(([, r]) => r.status === 'completed' && r.rows > 0);
  expect(ok.length, `atlas sources: ${JSON.stringify(V3!.atlas)}`).toBeGreaterThanOrEqual(8);
  expect(V3!.atlas_snapshot.origin).toBe('test_fixture');
  expect(V3!.atlas_snapshot.tables).toBeGreaterThan(0);
});

test('Neurones from Brazil (Q155): real neighbours, Wikidata citations "communautaire, non vérifié", expansion of a neighbour', async ({ page }) => {
  const api = await apiGet(page.request, '/v1/kg/entities/Q155/neighbours?direction=both&limit=80');
  expect(api.data.edges.length).toBeGreaterThan(0);
  expect(api.origin).toBe('test_fixture');

  const root = page.waitForResponse((r) => r.url().includes('/v1/kg/entities/Q155/neighbours'), SLOW);
  await page.goto('/v3/neurones?qid=Q155');
  expect((await root).status()).toBe(200);
  await expect(page.getByRole('heading', { level: 1, name: /Neurones/ })).toBeVisible(SLOW);
  const card = page.getByTestId('entity-card');
  await expect(card.getByRole('heading', { name: /Brésil/ })).toBeVisible(SLOW);
  await expect(card.getByTestId('community-badge')).toContainText(COMMUNITY);
  await expect(page.getByTestId('neuron-canvas')).toHaveAttribute('aria-label', /Graphe de \d+ entités/, SLOW);
  await expect.poll(async () => (await graphCounts(page))[1], SLOW).toBe(api.data.edges.length);
  const [nodesBefore] = await graphCounts(page);
  expect(nodesBefore).toBeGreaterThan(1);

  // Every statement shown in the side panel is cited (Wikidata statement link + community badge).
  const cites = page.locator('.v3-statements').getByTestId('citation');
  await expect(cites.first()).toBeVisible(SLOW);
  expect(await cites.count()).toBeGreaterThan(0);
  await expect(cites.first()).toContainText(COMMUNITY);
  await expect(cites.first().getByRole('link').first()).toHaveAttribute('href', /^https:\/\/www\.wikidata\.org\/wiki\/Q\d+#Q\d+\$/i);

  // Expand "portugais" (Q5146) from the keyboard-accessible node list: its neighbours come from the API
  // and add Christopher Columbus (Q7322), which is not a neighbour of Brazil.
  const expanded = page.waitForResponse((r) => r.url().includes('/v1/kg/entities/Q5146/neighbours'), SLOW);
  await page.getByText(/Liste des nœuds/).click();
  await page.locator('.v3-node-list li', { hasText: 'Q5146' }).getByRole('button').click();
  expect((await expanded).status()).toBe(200);
  await expect(page.getByTestId('entity-card').getByRole('heading', { name: /portugais/i })).toBeVisible(SLOW);
  await expect.poll(async () => (await graphCounts(page))[0], SLOW).toBeGreaterThan(nodesBefore);
  await expect(page.locator('.v3-node-list')).toContainText('Q7322', SLOW);
  await expect(page.locator('.v3-node-list li', { hasText: 'Q5146' })).toContainText('déplié');
});

test('Chemin from Columbus (Q7322) to Portuguese (Q5146): paths with every step cited, "a path is not a cause" banner', async ({ page }) => {
  const api = await apiGet(page.request, '/v1/links/path?from=Q7322&to=Q5146&k=3&max_hops=4');
  expect(api.data.found).toBe(true);
  expect(api.data.paths.length).toBeGreaterThan(0);

  await page.goto('/v3/chemin?from=Q7322&to=Q5146');
  await expect(page.getByTestId('path-not-cause')).toContainText('Un chemin n’est pas une cause.', SLOW);
  const result = page.getByTestId('paths-result');
  await expect(result).toBeVisible(SLOW);
  await expect(result).toContainText('liens typés (LINKS)'); // the real LINKS engine answered (no KG fallback)
  const chains = page.getByTestId('path-chain');
  await expect(chains).toHaveCount(api.data.paths.length, SLOW);
  const hops = await page.getByTestId('path-hop').all();
  expect(hops.length).toBe(api.data.paths.reduce((n: number, p: any) => n + p.hops.length, 0));
  for (const hop of hops) {
    const cites = hop.getByTestId('citation');
    expect(await cites.count(), `hop "${await hop.textContent()}" has no citation`).toBeGreaterThan(0);
    await expect(cites.first()).toContainText(COMMUNITY);
    await expect(hop).not.toContainText('aucune citation');
  }
  // No causal hop in this fixture chain: every step is worded "relié via", never as a cause.
  await expect(page.getByTestId('causal-wording')).toHaveCount(0);
  await expect(page.getByTestId('path-hop').first()).toContainText('relié via');
  await expect(chains.first()).toContainText('Christophe Colomb');
});

test('Frise of Brazil (Q155): dated statements on the SVG timeline, each cited', async ({ page }) => {
  const api = await apiGet(page.request, '/v1/kg/entities/Q155/timeline?include_incoming=true&limit=1000');
  const events = api.data.events as any[];
  // dated by qualifiers (start/end/point in time) or by their value (inception…): both are placed on the frise
  expect(events.filter((e) => e.start || e.end || e.point_in_time).length).toBeGreaterThan(0);

  await page.goto('/v3/frise?qid=Q155');
  await expect(page.getByRole('heading', { level: 1, name: /Frise — Brésil/ })).toBeVisible(SLOW);
  await expect(page.getByTestId('timeline-svg')).toBeVisible(SLOW);
  const heading = page.getByRole('heading', { name: /Éléments datés \((\d+)\)/ });
  await expect(heading).toBeVisible(SLOW);
  const shown = Number(/Éléments datés \((\d+)\)/.exec((await heading.textContent()) ?? '')![1]);
  expect(shown).toBeGreaterThan(0);
  expect(shown).toBeLessThanOrEqual(events.length);
  await heading.scrollIntoViewIfNeeded();
  const rows = page.locator('.v3-tl-row');
  await expect(rows.first()).toBeVisible(SLOW);
  for (const row of (await rows.all()).slice(0, 10)) {
    await expect(row.locator('.v3-tl-dates')).toHaveText(/\d{3,4}/);
    await expect(row.getByTestId('citation')).toHaveCount(1);
  }
  // Brazil's UN membership (1945) is a dated statement of the fixture.
  await expect(page.locator('.v3-tl-table')).toContainText('1945');
});

test('Atlas map with fixture data: values from the snapshot, hatched gaps with a reason (never zero), legend names the source', async ({ page }) => {
  const cov = await apiGet(page.request, `/v1/atlas/coverage?concept=${ATLAS_CONCEPT}`);
  const c = (cov.data as any[]).find((x) => x.concept_id === ATLAS_CONCEPT);
  expect(c?.countries, 'concept has fixture data').toBeGreaterThan(0);
  const map = await apiGet(page.request, `/v1/atlas/map?concept=${ATLAS_CONCEPT}&year=${c.year_max}`);
  const gaps: string[] = map.data.countries_without_value;
  const series = Object.keys(map.data.by_source);
  expect(gaps.length).toBeGreaterThan(0);
  expect(gaps).toContain('FRA');
  expect(series.length).toBeGreaterThan(0);

  await page.goto(`/v3/atlas?concept=${ATLAS_CONCEPT}`);
  const legend = page.getByTestId('atlas-legend');
  await expect(legend).toBeVisible(SLOW);
  await expect(legend).toContainText(`Légende — ${c.year_max}`);
  await expect(legend).toContainText(`manquant — ${gaps.length} pays`);
  for (const s of series) await expect(legend).toContainText(s);
  await expect(legend).toContainText('donnée de test');

  const ranking = page.getByTestId('atlas-ranking');
  await expect(ranking.getByRole('row').first()).toBeVisible(SLOW);
  await expect(page.getByRole('heading', { name: new RegExp(`Classement ${c.year_max} \\(${map.data.countries_with_value} pays`) })).toBeVisible();
  await expect(ranking.locator('[data-iso3="FRA"]')).toHaveCount(0); // a gap is never ranked (no zero)

  await page.getByText(new RegExp(`Pays sans valeur en ${c.year_max}`)).click();
  const fra = page.locator('[data-gap-country="FRA"]');
  await expect(fra).toContainText('manquant (');
  await expect(fra.locator('[data-missing-reason]')).toBeVisible();
  expect(await fra.textContent()).not.toMatch(/(^|\s)0([,.]0+)?(\s|$)/);
  await expect(page.getByTestId('atlas-gaps').locator('[data-gap-country]')).toHaveCount(gaps.length);

  // The map renders and a hatched country explains itself on hover (Europe/North America have no value here).
  const mapEl = page.getByTestId('atlas-map');
  await expect(mapEl.locator('canvas')).toBeAttached(SLOW);
  await mapEl.scrollIntoViewIfNeeded();
  await page.waitForTimeout(1500);
  const box = (await mapEl.boundingBox())!;
  const tip = page.getByTestId('atlas-map-tip');
  let found = '';
  outer: for (let fy = 0.2; fy <= 0.6; fy += 0.05) {
    for (let fx = 0.1; fx <= 0.9; fx += 0.05) {
      await page.mouse.move(box.x + box.width * fx, box.y + box.height * fy);
      if (await tip.count()) {
        const t = (await tip.textContent()) ?? '';
        if (t.includes('donnée manquante')) {
          found = t;
          break outer;
        }
      }
    }
  }
  expect(found, 'hovering a hatched country shows its gap reason').toContain(`donnée manquante ${c.year_max}`);
  expect(found).toContain('pas zéro');
});

test('Dossier pays FRA: several domains with values, source and year; missing variables listed with the sources consulted', async ({ page }) => {
  const api = await apiGet(page.request, '/v1/atlas/countries/FRA');
  const filled = (api.data.domains as any[]).filter((d) => d.concepts.length > 0);
  expect(filled.length).toBeGreaterThanOrEqual(3);
  const labour = (api.data.domains as any[]).find((d) => d.domain === 'labour');
  expect(labour?.concepts.length).toBeGreaterThan(0);
  expect(labour?.without_data.length).toBeGreaterThan(0);

  await page.goto('/v3/pays/FRA');
  await expect(page.getByRole('heading', { level: 1, name: /France/ })).toBeVisible(SLOW);
  await expect(page.locator('.v3-meta').first()).toContainText(`${api.data.concepts_with_data} variables renseignées`, SLOW);
  await expect(page.locator('.v3-meta').first()).toContainText('donnée de test');

  // Several domains render filled rows; each row has a value, a year and a cited source.
  const sections = page.locator('section.v3-domain[data-domain]');
  await expect(sections.first()).toBeVisible(SLOW);
  let checked = 0;
  for (const d of filled.slice(0, 4)) {
    const sec = page.locator(`section.v3-domain[data-domain="${d.domain}"]`);
    const cell = d.concepts[0];
    const row = sec.locator(`tr[data-concept="${cell.concept_id}"]`);
    await expect(row).toBeVisible(SLOW);
    await expect(row.locator('td').nth(1)).toContainText(String(cell.latest.year ?? cell.latest.period_start.slice(0, 4)));
    await expect(row.getByTestId('citation')).toContainText(cell.latest.source);
    await expect(row.locator('td').first()).not.toContainText('manquant');
    checked++;
  }
  expect(checked).toBeGreaterThanOrEqual(3);

  // The minimum wage rows come from the fixture sources (ILOSTAT / OECD) with their year.
  const mw = labour.concepts.find((x: any) => x.concept_id === 'labour.minimum_wage.monthly.local_currency');
  if (mw) {
    const row = page.locator('tr[data-concept="labour.minimum_wage.monthly.local_currency"]');
    await expect(row).toContainText(String(mw.latest.year));
    await expect(row.getByTestId('citation')).toContainText(mw.latest.source);
  }

  // Missing variables of the labour domain, with the sources searched; never a zero.
  const lab = page.locator('section.v3-domain[data-domain="labour"]');
  await lab.getByText(/Sans donnée pour ce pays/).click();
  const missing = page.getByTestId('missing-labour');
  await expect(missing.locator('[data-missing-concept]')).toHaveCount(labour.without_data.length);
  const first = missing.locator(`[data-missing-concept="${labour.without_data[0]}"]`);
  await expect(first).toContainText('manquant (');
  await expect(first).toContainText('sources consultées :');
});

test('Demander "Quel est le salaire minimum en France ?": keyword planner, every sentence cited or the gap said, nothing invented', async ({ page }) => {
  const q = 'Quel est le salaire minimum en France ?';
  await page.goto('/v3/demander');
  await expect(page.getByTestId('sourced-only')).toBeVisible(SLOW);
  await page.getByLabel('Votre question').fill(q);
  const resp = page.waitForResponse((r) => r.url().endsWith('/v1/ask') && r.request().method() === 'POST', { timeout: 120_000 });
  await page.getByRole('button', { name: 'Demander', exact: true }).click();
  const r = await resp;
  expect(r.status()).toBe(200);
  const body = await r.json();
  expect(body.planner ?? body.plan.planner).toBe('keyword_fallback');
  expect(body.warnings.join(' ')).toContain('llm_not_enabled_for_questions');

  const answer = page.getByTestId('ask-answer');
  await expect(answer).toBeVisible(SLOW);
  const sentences = page.getByTestId('answer-sentence');
  await expect(sentences).toHaveCount(body.sentences.length);
  const evidence = new Map<string, any>((body.evidence as any[]).map((e) => [e.id, e]));
  await expect(page.getByTestId('evidence-item')).toHaveCount(evidence.size);

  // Every factual sentence cites evidence that the server returned; the UI shows no "sans citation".
  for (const s of body.sentences as any[]) {
    if (['fact', 'identification'].includes(s.kind)) {
      expect(s.citations.length, `uncited sentence: ${s.text}`).toBeGreaterThan(0);
      for (const id of s.citations) expect(evidence.has(id), `sentence ${s.id} cites missing evidence ${id}`).toBe(true);
    }
  }
  await expect(answer.locator('.v3-badge-uncited')).toHaveCount(0);

  // Either the minimum wage is answered from a cited atlas cell of France, or the answer says it is not covered.
  const atlasCells = (body.evidence as any[]).filter((e) => e.kind === 'atlas_cell' && e.iso3 === 'FRA' && String(e.concept_id ?? '').includes('minimum_wage'));
  const wageSentences = (body.sentences as any[]).filter((s) => /salaire minimum|minimum wage/i.test(s.text) && s.kind !== 'unsupported');
  if (atlasCells.length > 0) {
    expect(wageSentences.some((s) => s.citations.some((id: string) => atlasCells.some((e) => e.id === id)))).toBe(true);
    // Data present: never worded as absent (a concept tie is a choice or an ambiguity, not "not covered").
    await expect(answer).not.toContainText('Non couvert par les données de REVA');
  } else {
    expect((body.sentences as any[]).some((s) => s.kind === 'unsupported' || s.kind === 'missing') || body.unsupported.length > 0).toBe(true);
    await expect(answer).toContainText(/Non couvert|manquant|absente|ne couvre pas/);
    // Nothing invented: no sentence states a wage figure without an atlas cell behind it.
    for (const s of wageSentences) expect(s.text).not.toMatch(/\d[\d\s.,]*\s*(€|EUR|euros?)/i);
  }
  await page.getByText(/Plan de requête/).click();
  await expect(page.locator('.v3-plan')).toContainText('keyword_fallback');
});

test('V3 routes refuse unauthenticated calls (the live specs above use a real session)', async ({ browser }) => {
  const ctx = await browser.newContext({ baseURL: BASE_URL, storageState: { cookies: [], origins: [] } });
  const r = await ctx.request.get('/v1/kg/entities/Q155');
  expect([401, 403]).toContain(r.status());
  const a = await ctx.request.post('/v1/ask', { data: { question: 'x' }, headers: { 'Content-Type': 'application/json' } });
  expect([401, 403]).toContain(a.status());
  await ctx.close();
});
