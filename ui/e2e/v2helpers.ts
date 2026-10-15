/**
 * Helpers for the V2 end-to-end specs. Everything goes through the real API of the e2e stack
 * (no route mocking): scenarios are created with POST /v1/scenarios and run by the stack's
 * real worker.
 */
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { expect, type APIRequestContext, type BrowserContext } from '@playwright/test';
import { RELEASE, apiGet, csrfToken } from './helpers';

export async function apiPost(request: APIRequestContext, context: BrowserContext, path: string, data: unknown): Promise<{ status: number; body: any }> {
  const token = await csrfToken(context);
  const r = await request.post(path, {
    data,
    headers: { 'X-CSRF-Token': token, 'Idempotency-Key': crypto.randomUUID(), 'Content-Type': 'application/json' },
  });
  let body: any = null;
  try {
    body = await r.json();
  } catch {
    body = await r.text();
  }
  return { status: r.status(), body };
}

/** The crude example spec pinned to the stack's release (never the placeholder UUID). */
export function crudeSpec(overrides: Record<string, unknown> = {}): Record<string, unknown> {
  const path = fileURLToPath(new URL('../../contracts/scenario.example.json', import.meta.url));
  const spec = JSON.parse(readFileSync(path, 'utf-8')) as Record<string, unknown>;
  if (!RELEASE) throw new Error('stack file has no release');
  return { ...spec, baseline_release_id: RELEASE.id, knowledge_cutoff: RELEASE.knowledge_cutoff, ...overrides };
}

/** Create a scenario and run it; waits until the job is terminal and returns the job id. */
export async function runScenario(request: APIRequestContext, context: BrowserContext, spec: Record<string, unknown>, timeout = 120_000): Promise<string> {
  const sc = await apiPost(request, context, '/v1/scenarios', spec);
  expect([200, 201], `POST /v1/scenarios: ${JSON.stringify(sc.body)}`).toContain(sc.status); // 200 = identical spec already exists
  const sim = await apiPost(request, context, '/v1/simulations', { scenario_id: sc.body.scenario_id, fidelity: spec.fidelity ?? 'L1' });
  expect(sim.status, `POST /v1/simulations: ${JSON.stringify(sim.body)}`).toBe(202);
  const id = sim.body.job_id as string;
  const t0 = Date.now();
  for (;;) {
    const job = await apiGet(request, `/v1/jobs/${id}`);
    if (['succeeded', 'failed', 'cancelled', 'dead'].includes(job.status)) break;
    if (Date.now() - t0 > timeout) throw new Error(`job ${id} not finished after ${timeout} ms`);
    await new Promise((r) => setTimeout(r, 500));
  }
  return id;
}
