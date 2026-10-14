import { vi } from 'vitest';

export interface Call {
  url: string;
  method: string;
  headers: Headers;
  body: unknown;
  credentials: RequestCredentials | undefined;
}

type Handler = (call: Call) => { status?: number; body?: unknown; contentType?: string } | undefined;

/** Install a fetch mock routing by "METHOD path" prefix; records every call. */
export function mockFetch(routes: Record<string, Handler | { status?: number; body?: unknown }>) {
  const calls: Call[] = [];
  const fn = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = typeof input === 'string' ? input : input instanceof URL ? input.toString() : input.url;
    const method = (init?.method ?? 'GET').toUpperCase();
    const headers = new Headers(init?.headers);
    const body = typeof init?.body === 'string' ? JSON.parse(init.body) : undefined;
    const call: Call = { url, method, headers, body, credentials: init?.credentials };
    calls.push(call);
    const path = url.split('?')[0];
    const key = Object.keys(routes)
      .filter((k) => {
        const [m, p] = k.split(' ');
        return m === method && path !== undefined && path.startsWith(p!);
      })
      .sort((a, b) => b.length - a.length)[0];
    const route = key ? routes[key] : undefined;
    const res = typeof route === 'function' ? route(call) : route;
    if (!res) {
      return new Response(JSON.stringify({ type: 'about:blank', title: 'not found', status: 404, detail: `no mock for ${method} ${path}`, code: 'not_found' }), {
        status: 404,
        headers: { 'Content-Type': 'application/problem+json' },
      });
    }
    const status = res.status ?? 200;
    return new Response(status === 204 ? null : JSON.stringify(res.body ?? {}), {
      status,
      headers: { 'Content-Type': (res as { contentType?: string }).contentType ?? 'application/json' },
    });
  });
  vi.stubGlobal('fetch', fn);
  return { calls, fn };
}

export function envelope<T>(data: T, extra: Record<string, unknown> = {}) {
  return {
    data,
    context_id: null,
    context_mode: 'belief',
    origin: 'live',
    modality: 'selected_estimate',
    estimand: null,
    units: [],
    support: null,
    baseline_release: null,
    knowledge_cutoff: null,
    valid_time: null,
    model_versions: [],
    assumptions: [],
    uncertainty: { status: 'not_applicable' },
    data_freshness: null,
    evidence_profile: { status: 'not_applicable' },
    applicability: { status: 'not_applicable' },
    numerical_error: { status: 'not_applicable' },
    compression_error: { status: 'not_applicable' },
    model_disagreement: { status: 'not_applicable' },
    historical_validation: { status: 'not_applicable' },
    provenance_links: [],
    warnings: [],
    cost: null,
    next_cursor: null,
    ...extra,
  };
}
