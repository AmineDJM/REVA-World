/**
 * Low-level typed fetch for the REVA API.
 *
 * - Session auth uses an HttpOnly cookie; requests send `credentials: 'same-origin'`.
 * - No token is ever stored in localStorage/sessionStorage.
 * - Unsafe methods carry `X-CSRF-Token`, read from the (non-HttpOnly) `reva_csrf` cookie.
 * - Work-starting POSTs carry an `Idempotency-Key` (crypto.randomUUID()).
 * - Errors are RFC 9457 problem details wrapped in `ApiError`.
 */
import { ApiError, kindForStatus, toProblem } from './problem';

export const CSRF_COOKIE = 'reva_csrf';
export const CSRF_HEADER = 'X-CSRF-Token';
export const IDEMPOTENCY_HEADER = 'Idempotency-Key';

const UNSAFE = new Set(['POST', 'PUT', 'PATCH', 'DELETE']);

export type Query = Record<string, string | number | boolean | null | undefined | readonly (string | number)[]>;

export interface RequestOptions {
  query?: Query;
  body?: unknown;
  /**
   * For POSTs that start work. Pass `true` to generate a fresh key, or a string
   * to reuse a key when retrying the same logical submission.
   */
  idempotencyKey?: string | true;
  signal?: AbortSignal;
}

export interface ApiResponse<T> {
  status: number;
  body: T;
  headers: Headers;
}

export function readCookie(name: string, cookieString: string = typeof document !== 'undefined' ? document.cookie : ''): string | undefined {
  for (const part of cookieString.split(';')) {
    const idx = part.indexOf('=');
    if (idx < 0) continue;
    const key = part.slice(0, idx).trim();
    if (key === name) {
      try {
        return decodeURIComponent(part.slice(idx + 1).trim());
      } catch {
        return part.slice(idx + 1).trim();
      }
    }
  }
  return undefined;
}

export function newIdempotencyKey(): string {
  return crypto.randomUUID();
}

export function buildUrl(path: string, query?: Query): string {
  const params = new URLSearchParams();
  if (query) {
    for (const [k, v] of Object.entries(query)) {
      if (v === undefined || v === null || v === '') continue;
      if (Array.isArray(v)) {
        for (const item of v) params.append(k, String(item));
      } else {
        params.append(k, String(v));
      }
    }
  }
  const qs = params.toString();
  return qs ? `${path}?${qs}` : path;
}

type UnauthorizedListener = () => void;
const unauthorizedListeners = new Set<UnauthorizedListener>();

/** Subscribe to 401 responses (used by the app shell to show the login page). */
export function onUnauthorized(fn: UnauthorizedListener): () => void {
  unauthorizedListeners.add(fn);
  return () => unauthorizedListeners.delete(fn);
}

export async function request<T>(method: string, path: string, opts: RequestOptions = {}): Promise<ApiResponse<T>> {
  const m = method.toUpperCase();
  const headers = new Headers({ Accept: 'application/json, application/problem+json' });
  if (opts.body !== undefined) headers.set('Content-Type', 'application/json');
  if (UNSAFE.has(m)) {
    const csrf = readCookie(CSRF_COOKIE);
    if (csrf) headers.set(CSRF_HEADER, csrf);
  }
  if (opts.idempotencyKey) {
    headers.set(IDEMPOTENCY_HEADER, opts.idempotencyKey === true ? newIdempotencyKey() : opts.idempotencyKey);
  }

  let res: Response;
  try {
    res = await fetch(buildUrl(path, opts.query), {
      method: m,
      headers,
      credentials: 'same-origin',
      body: opts.body === undefined ? undefined : JSON.stringify(opts.body),
      signal: opts.signal,
    });
  } catch (e) {
    if (e instanceof DOMException && e.name === 'AbortError') throw e;
    throw new ApiError('unreachable', 0, {
      title: 'API unreachable',
      detail: 'The REVA API did not respond. Check that the API process is running.',
    });
  }

  const text = res.status === 204 ? '' : await res.text();
  let parsed: unknown = undefined;
  if (text) {
    try {
      parsed = JSON.parse(text);
    } catch {
      if (res.ok) {
        throw new ApiError('invalid_response', res.status, {
          title: 'Invalid API response',
          detail: 'The API returned a body that is not JSON.',
        });
      }
    }
  }

  if (!res.ok) {
    const problem = toProblem(res.status, parsed, res.statusText || `HTTP ${res.status}`);
    if (res.status === 401) unauthorizedListeners.forEach((fn) => fn());
    throw new ApiError(kindForStatus(res.status), res.status, problem);
  }
  return { status: res.status, body: parsed as T, headers: res.headers };
}
