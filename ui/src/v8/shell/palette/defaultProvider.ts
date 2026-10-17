/**
 * Default palette provider: GET /v1/v8/search (V8API universal index). When that route is absent (404)
 * or its index is not built (503 search_index_missing), fall back silently to the search that exists
 * today: /v1/kg/search (entities) + the atlas catalogue (/v1/atlas/concepts, memoised, filtered here).
 */
import { z } from 'zod';
import { request } from '../../../api/http';
import { ApiError } from '../../../api/problem';
import { entityLabel, searchKg } from '../../../v3/api/kg';
import { getAtlasCatalog } from '../../../v3/api/atlas';
import { v8Links, type PaletteHit } from '../../contract';
import { bestScore } from './fuzzy';

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

const V8Hit = z.looseObject({
  id: z.string(),
  label: z.string(),
  sublabel: z.string().nullish(),
  href: z.string(),
  group: z.string().nullish(),
  kind: z.string().nullish(),
});
const V8Search = z.looseObject({ data: z.looseObject({ hits: z.array(V8Hit) }) });

const isAbort = (e: unknown) => e instanceof DOMException && e.name === 'AbortError';

async function v8Search(q: string, signal: AbortSignal): Promise<PaletteHit[]> {
  const res = await request<unknown>('GET', '/v1/v8/search', { query: { q, limit: 24, lang: 'fr' }, signal });
  const body = V8Search.parse(res.body);
  return body.data.hits
    .filter((h) => h.href.startsWith('/'))
    .map((h) => ({ id: `${h.kind ?? 'hit'}:${h.id}`, label: h.label, sublabel: h.sublabel ?? undefined, href: h.href, group: h.group ?? 'Résultats' }));
}

const KIND_FR: Record<string, string> = {
  country: 'Pays',
  region: 'Région',
  person: 'Personne',
  organization: 'Organisation',
  company: 'Entreprise',
  event: 'Événement',
  drug: 'Médicament',
  gene: 'Gène',
  protein: 'Protéine',
  disease: 'Maladie',
  taxon: 'Taxon',
  element: 'Élément',
  material: 'Matériau',
  commodity: 'Produit de base',
  place: 'Lieu',
};

export async function fallbackSearch(q: string, signal: AbortSignal): Promise<PaletteHit[]> {
  const [kg, catalog] = await Promise.allSettled([searchKg(q, { limit: 10 }, signal), getAtlasCatalog()]);
  if (signal.aborted) throw new DOMException('aborted', 'AbortError');
  const out: PaletteHit[] = [];
  if (kg.status === 'fulfilled') {
    for (const h of kg.value.data.results) {
      const kind = h.kind ?? 'entity';
      out.push({
        id: `kg:${h.qid}`,
        label: entityLabel(h),
        sublabel: [KIND_FR[kind] ?? kind, h.qid].join(' · '),
        href: v8Links.entity(kind, h.qid),
        group: kind === 'country' || kind === 'region' ? 'Pays & régions' : 'Entités',
      });
    }
  }
  if (catalog.status === 'fulfilled') {
    const scored = catalog.value.data
      .map((c) => ({ c, s: bestScore(q, [c.labels.fr, c.labels.en, c.id]) }))
      .filter((x): x is { c: (typeof x)['c']; s: number } => x.s !== null && x.s >= 600)
      .sort((a, b) => b.s - a.s)
      .slice(0, 8);
    for (const { c } of scored) {
      out.push({
        id: `concept:${c.id}`,
        label: c.labels.fr || c.labels.en || c.id,
        sublabel: [c.domain, c.unit].filter(Boolean).join(' · '),
        href: v8Links.series(c.source_order[0] ?? c.id, { concept: c.id }),
        group: 'Indicateurs',
      });
    }
  }
  if (kg.status === 'rejected' && catalog.status === 'rejected') throw kg.reason;
  return out;
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
  return fallbackSearch(q, signal);
}
