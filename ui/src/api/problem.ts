/**
 * RFC 9457 problem details as produced by the REVA API (`RevaError.problem()`),
 * plus a client-side `unreachable` kind for network failures.
 */
export interface ProblemDetails {
  type?: string;
  title?: string;
  status?: number;
  detail?: string;
  instance?: string;
  code?: string;
  details?: Record<string, unknown>;
  [extension: string]: unknown;
}

export type ApiErrorKind =
  | 'unreachable' // network failure or no response
  | 'unauthenticated' // 401
  | 'forbidden' // 403
  | 'not_found' // 404
  | 'conflict' // 409
  | 'unprocessable' // 422 (validation, capability_gap, missing_input, ...)
  | 'rate_limited' // 429
  | 'server' // 5xx
  | 'invalid_response' // non-JSON or malformed body
  | 'other';

export class ApiError extends Error {
  readonly kind: ApiErrorKind;
  readonly status: number;
  readonly problem: ProblemDetails;

  constructor(kind: ApiErrorKind, status: number, problem: ProblemDetails) {
    super(problem.detail ?? problem.title ?? `API error (${status || kind})`);
    this.name = 'ApiError';
    this.kind = kind;
    this.status = status;
    this.problem = problem;
  }

  /** Stable machine code, e.g. `capability_gap`, `missing_input`, `validation_error`. */
  get code(): string | undefined {
    return typeof this.problem.code === 'string' ? this.problem.code : undefined;
  }
}

export function kindForStatus(status: number): ApiErrorKind {
  if (status === 401) return 'unauthenticated';
  if (status === 403) return 'forbidden';
  if (status === 404) return 'not_found';
  if (status === 409) return 'conflict';
  if (status === 422) return 'unprocessable';
  if (status === 429) return 'rate_limited';
  if (status >= 500) return 'server';
  return 'other';
}

/** Parse a response body into problem details without trusting its shape. */
export function toProblem(status: number, body: unknown, fallbackTitle: string): ProblemDetails {
  if (body && typeof body === 'object' && !Array.isArray(body)) {
    const b = body as Record<string, unknown>;
    // FastAPI default validation errors use {"detail": [...]}.
    if (Array.isArray(b.detail)) {
      return {
        title: typeof b.title === 'string' ? b.title : 'Validation error',
        status,
        code: typeof b.code === 'string' ? b.code : 'validation_error',
        detail: (b.detail as unknown[])
          .map((d) => {
            if (d && typeof d === 'object') {
              const o = d as Record<string, unknown>;
              const loc = Array.isArray(o.loc) ? o.loc.join('.') : '';
              return `${loc ? `${loc}: ` : ''}${String(o.msg ?? '')}`;
            }
            return String(d);
          })
          .join('; '),
        details: { errors: b.detail },
      };
    }
    return { status, ...b, title: typeof b.title === 'string' ? b.title : fallbackTitle };
  }
  return { status, title: fallbackTitle };
}
