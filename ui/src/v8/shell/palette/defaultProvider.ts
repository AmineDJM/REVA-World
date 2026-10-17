/**
 * Default palette provider: GET /v1/v8/search (V8API universal index). When that route is absent (404)
 * or its index is not built (503 search_index_missing), fall back silently to the search that exists
 * today: /v1/kg/search (entities) + the atlas catalogue (/v1/atlas/concepts, memoised, filtered here).
 */
import { request } from '../../../api/http';
import { ApiError } from '../../../api/problem';
import type { PaletteHit } from '../../contract';

export type SearchBackend = 'unknown' | 'v8' | 'fallback';

let backend: SearchBackend = 'unknown';
let v8RetryAt = 0;
const listeners = new Set<(b: SearchBackend) => void>();

export function searchBackend(): SearchBackend {
  return backend;
}
export function onSearchBackend(fn: (b: SearchBackend) => void): () => void {
  listeners.add(fn);
  return () => listeners.delete(fn);
}
function setBackend(b: SearchBackend) {
  if (b === backend) return;
  backend = b;
  listeners.forEach((fn) => fn(b));
}
/** Tests only. */
export function resetSearchBackend() {
  backend = 'unknown';
  v8RetryAt = 0;
}

interface V8Hit {
  id: string;
  label: string;
  sublabel?: string | null;
  href: string;
  group?: string | null;
  kind?: string | null;
}

/** Tolerant check of GET /v1/v8/search (docs/dev/v8_api_interface.md); a malformed hit is dropped, never guessed. */
export function parseV8Hits(body: unknown): V8Hit[] {
  const hits = (body as { data?: { hits?: unknown } } | null)?.data?.hits;
  if (!Array.isArray(hits)) throw new Error('réponse /v1/v8/search non conforme');
  const str = (v: unknown) => typeof v === 'string';
  return hits.filter((h): h is V8Hit => !!h && typeof h === 'object' && str(h.id) && str(h.label) && str(h.href));
}

const isAbort = (e: unknown) => e instanceof DOMException && e.name === 'AbortError';

async function v8Search(q: string, signal: AbortSignal): Promise<PaletteHit[]> {
  const res = await request<unknown>('GET', '/v1/v8/search', { query: { q, limit: 24, lang: 'fr' }, signal });
  return parseV8Hits(res.body)
    .filter((h) => h.href.startsWith('/'))
    .map((h) => ({ id: `${h.kind ?? 'hit'}:${h.id}`, label: h.label, sublabel: h.sublabel ?? undefined, href: h.href, group: h.group ?? 'Résultats' }));
}

export async function defaultSearch(q: string, signal: AbortSignal): Promise<PaletteHit[]> {
  if (backend !== 'fallback' || Date.now() >= v8RetryAt) {
    try {
      const hits = await v8Search(q, signal);
      setBackend('v8');
      return hits;
    } catch (e) {
      if (isAbort(e)) throw e;
      // 404 route absent → stay on the fallback for this page view; 503 index missing / other → retry in 60 s.
      const absent = e instanceof ApiError && e.status === 404;
      v8RetryAt = absent ? Number.POSITIVE_INFINITY : Date.now() + 60_000;
      setBackend('fallback');
    }
  }
  const { fallbackSearch } = await import('./fallback'); // kg + atlas clients (zod) load only when needed
  return fallbackSearch(q, signal);
}
