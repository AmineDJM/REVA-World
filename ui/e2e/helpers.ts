/**
 * Shared configuration for the REVA end-to-end suite.
 *
 * The stack comes from `uv run python scripts/e2e_stack.py` (fresh database, captured
 * fixtures, test_fixture release, built UI, API + real worker). Connection details are
 * read from the environment (REVA_E2E_BASE_URL / _USERNAME / _PASSWORD /
 * _STORAGE_STATE, set by `e2e_stack.py --run`) or from the stack file it writes
 * (REVA_E2E_STACK_FILE, default ../var/e2e/stack.json).
 *
 * Only one browser login per run is made by the stack itself (storageState); the auth
 * spec adds two more (wrong + right password). This keeps the always-on login rate
 * limit from being exhausted.
 */
import { existsSync, readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { expect, type APIRequestContext, type BrowserContext, type Page } from '@playwright/test';

interface StackFile {
  base_url: string;
  username: string;
  password: string;
  storage_state: string;
  database?: string;
  release?: { id: string; knowledge_cutoff: string; sequence: number; origin: string };
}

function readStack(): StackFile | null {
  const path = process.env.REVA_E2E_STACK_FILE ?? fileURLToPath(new URL('../../var/e2e/stack.json', import.meta.url));
  if (!existsSync(path)) return null;
  try {
    return JSON.parse(readFileSync(path, 'utf-8')) as StackFile;
  } catch {
    return null;
  }
}

const stack = readStack();

export const BASE_URL = process.env.REVA_E2E_BASE_URL ?? stack?.base_url ?? '';
export const USERNAME = process.env.REVA_E2E_USERNAME ?? stack?.username ?? 'admin';
export const PASSWORD = process.env.REVA_E2E_PASSWORD ?? stack?.password ?? '';
export const STORAGE_STATE = process.env.REVA_E2E_STORAGE_STATE ?? stack?.storage_state ?? '';
export const RELEASE = stack?.release ?? null;
export const SKIP_REASON = 'No REVA e2e stack: run `uv run python scripts/e2e_stack.py` or set REVA_E2E_BASE_URL';
export const HAVE_STACK = !!BASE_URL;
export const HAVE_SESSION = HAVE_STACK && !!STORAGE_STATE && existsSync(STORAGE_STATE);

/** The placeholder baseline in contracts/scenario.example.json; never a real release. */
export const FIXTURE_BASELINE_UUID = '00000000-0000-4000-8000-000000000001';

export const SCREENS = ['/world', '/country/FRA', '/country/DZA', '/evidence', '/graph', '/simulation', '/models', '/acquisition'];

/** Wait until no "Loading …" status remains on the page (bounded). */
export async function settle(page: Page, timeout = 30_000) {
  await expect(page.locator('.state-loading')).toHaveCount(0, { timeout });
}

export async function csrfToken(context: BrowserContext): Promise<string> {
  const c = (await context.cookies()).find((x) => x.name === 'reva_csrf');
  if (!c) throw new Error('no reva_csrf cookie in the browser context');
  return c.value;
}

/** Authenticated JSON GET through the page's cookie jar (same session as the UI). */
export async function apiGet<T = any>(request: APIRequestContext, path: string): Promise<T> {
  const r = await request.get(path);
  expect(r.status(), `GET ${path}: ${await r.text().catch(() => '')}`).toBe(200);
  return (await r.json()) as T;
}
