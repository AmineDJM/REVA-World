/**
 * Shared plumbing for the V3 API clients (knowledge graph, atlas, links, ask).
 *
 * - Every response is validated with zod against the shapes of the real routes
 *   (src/reva/api/routes/{kg,atlas,links,ask}.py). Schemas are *tolerant* (unknown fields pass
 *   through, optional members may be absent) but *strict on honesty-critical members*: an atlas
 *   value must be a decimal string or null (never a JSON number that could hide a filled zero),
 *   every KG statement must carry its provenance.
 * - A response that does not match is a `ContractError`; the UI shows it, it never renders a
 *   partial or invented result.
 * - `classifyV3Error` turns 404 (route absent) / 501 into "pas encore disponible", a missing atlas
 *   snapshot into "aucune donnée", an unknown entity into "introuvable".
 */
import { z } from 'zod';
import { request, type Query } from '../../api/http';
import { ApiError } from '../../api/problem';

export class ContractError extends Error {
  readonly endpoint: string;
  readonly issues: string[];
  constructor(endpoint: string, issues: string[]) {
    super(`Réponse non conforme au contrat attendu (${endpoint})`);
    this.name = 'ContractError';
    this.endpoint = endpoint;
    this.issues = issues;
  }
}

export function issuesOf(err: z.ZodError): string[] {
  return err.issues.slice(0, 12).map((i) => `${i.path.length ? i.path.join('.') : '(racine)'} : ${i.message}`);
}

/** Validate `body` with `schema`; throws ContractError with readable issues. */
export function parseWith<S extends z.ZodType>(schema: S, body: unknown, endpoint: string): z.output<S> {
  const r = schema.safeParse(body);
  if (!r.success) throw new ContractError(endpoint, issuesOf(r.error));
  return r.data;
}

export async function v3get<S extends z.ZodType>(path: string, schema: S, query?: Query, signal?: AbortSignal): Promise<z.output<S>> {
  const res = await request<unknown>('GET', path, { query, signal });
  return parseWith(schema, res.body, path);
}

export async function v3post<S extends z.ZodType>(path: string, schema: S, body: unknown, signal?: AbortSignal): Promise<z.output<S>> {
  const res = await request<unknown>('POST', path, { body, signal });
  return parseWith(schema, res.body, path);
}

/**
 * Session memo for static reads (the atlas catalog, display geometry): one request per page
 * load; a failure is not cached. The caller's AbortSignal is not passed to the shared request.
 */
export function memo<T>(fn: () => Promise<T>): () => Promise<T> {
  let p: Promise<T> | null = null;
  return () => {
    if (!p) {
      p = fn();
      p.catch(() => {
        p = null;
      });
    }
    return p;
  };
}

// ------------------------------------------------------------------ shared schemas

export const ApiWarning = z.looseObject({ code: z.string(), message: z.string() });
export type ApiWarning = z.output<typeof ApiWarning>;

/** The common response envelope (src/reva/api/schemas.py Envelope), tolerant. */
export function envelopeOf<D extends z.ZodType>(data: D) {
  return z.looseObject({
    data,
    origin: z.string().nullish(),
    context_mode: z.string().nullish(),
    modality: z.string().nullish(),
    knowledge_cutoff: z.string().nullish(),
    warnings: z.array(ApiWarning).nullish().transform((w) => w ?? []),
    next_cursor: z.string().nullish(),
  });
}

/** Strict decimal string as the atlas serves it ("-12.5", "3e-4"); never a JSON number. */
export const DecimalString = z.string().regex(/^[+-]?(\d+\.?\d*|\.\d+)([eE][+-]?\d+)?$/, 'valeur décimale attendue sous forme de chaîne');

/** Server warnings in French; the ones the screen already states (path ≠ cause) are dropped. */
const WARNING_FR: Record<string, string | null> = {
  path_not_causal: null,
  community_edited: 'Les déclarations Wikidata sont des contributions communautaires, non vérifiées par REVA.',
  fixture_data: 'Données de test (test_fixture), pas des données publiées.',
  truncated: 'Recherche ou liste bornée par le serveur : d’autres résultats peuvent exister.',
  display_only: 'Géométrie d’affichage seulement.',
};

export function warningsFr(ws: { code: string; message: string }[]): string[] {
  const out: string[] = [];
  for (const w of ws) {
    const t = w.code in WARNING_FR ? WARNING_FR[w.code] : w.message;
    if (t && !out.includes(t)) out.push(t);
  }
  return out;
}

export const JsonRecord = z.record(z.string(), z.unknown());

// ------------------------------------------------------------------ error classification

export type V3ErrorKind =
  | 'unavailable' // endpoint not deployed yet (404 route, 501, non-JSON)
  | 'no_data' // endpoint exists but nothing has been loaded (e.g. no atlas snapshot)
  | 'not_found' // the requested entity/country/concept is unknown
  | 'invalid' // 422: the request was refused (validation)
  | 'rate_limited' // 429
  | 'unreachable'
  | 'contract' // response did not match the expected shape
  | 'auth'
  | 'error';

export interface V3ErrorInfo {
  kind: V3ErrorKind;
  title: string;
  detail: string | null;
  status: number | null;
  code: string | null;
  issues?: string[];
}

const NO_DATA_CODES = new Set(['atlas_snapshot_unavailable', 'kg_empty', 'no_data', 'not_loaded']);

export function classifyV3Error(error: unknown): V3ErrorInfo {
  if (error instanceof ContractError) {
    return {
      kind: 'contract',
      title: 'Réponse non conforme',
      detail: `Le serveur a répondu (${error.endpoint}) mais pas dans la forme attendue ; rien n’est affiché plutôt qu’un résultat partiel.`,
      status: null,
      code: null,
      issues: error.issues,
    };
  }
  if (error instanceof ApiError) {
    const code = error.code ?? null;
    const detail = typeof error.problem.detail === 'string' ? error.problem.detail : null;
    const base = { status: error.status || null, code, detail };
    if (error.status === 501 || error.kind === 'invalid_response') {
      return { ...base, kind: 'unavailable', title: 'Pas encore disponible' };
    }
    if (error.status === 404) {
      // FastAPI's own 404 for an unknown route has the literal detail "Not Found".
      if (!detail || detail === 'Not Found') return { ...base, kind: 'unavailable', title: 'Pas encore disponible' };
      if (code && NO_DATA_CODES.has(code)) return { ...base, kind: 'no_data', title: 'Aucune donnée chargée' };
      return { ...base, kind: 'not_found', title: 'Introuvable' };
    }
    if (error.status === 422) return { ...base, kind: 'invalid', title: 'Requête refusée' };
    if (error.status === 429) return { ...base, kind: 'rate_limited', title: code === 'budget_exhausted' ? 'Budget quotidien épuisé' : 'Trop de requêtes' };
    if (error.kind === 'unreachable') return { ...base, kind: 'unreachable', title: 'API injoignable' };
    if (error.status === 401 || error.status === 403) return { ...base, kind: 'auth', title: 'Accès refusé' };
    return { ...base, kind: 'error', title: typeof error.problem.title === 'string' ? error.problem.title : 'Erreur' };
  }
  return { kind: 'error', title: 'Erreur', detail: error instanceof Error ? error.message : String(error), status: null, code: null };
}
