import type { ReactNode } from 'react';
import { ApiError } from '../api/problem';

export function Loading({ what }: { what: string }) {
  return (
    <p className="state state-loading" role="status" aria-live="polite">
      Loading {what}…
    </p>
  );
}

export function Empty({ what, children }: { what: string; children?: ReactNode }) {
  return (
    <div className="state state-empty" role="status">
      <p>
        <strong>No {what} returned by the API.</strong>
      </p>
      {children ?? <p>Nothing has been published for this view yet. This is an absence of records, not a zero value.</p>}
    </div>
  );
}

export function ErrorState({ error, what, onRetry }: { error: unknown; what: string; onRetry?: () => void }) {
  const api = error instanceof ApiError ? error : null;
  const title =
    api?.kind === 'unreachable'
      ? 'API unreachable'
      : api?.kind === 'unauthenticated'
        ? 'Sign-in required'
        : api?.kind === 'forbidden'
          ? 'Not permitted'
          : api?.kind === 'not_found'
            ? 'Not found'
            : api?.code === 'capability_gap'
              ? 'Unsupported request'
              : api?.code === 'missing_input'
                ? 'Missing input'
                : api?.problem.title ?? 'Request failed';
  const detail = api ? api.problem.detail : error instanceof Error ? error.message : String(error);
  const details = api?.problem.details;
  return (
    <div className="state state-error" role="alert">
      <p>
        <strong>{title}</strong> — {what} could not be loaded{api?.status ? ` (HTTP ${api.status}${api.code ? `, ${api.code}` : ''})` : ''}.
      </p>
      {detail && <p>{detail}</p>}
      {details && Object.keys(details).length > 0 && (
        <details>
          <summary>Problem details</summary>
          <pre className="json">{JSON.stringify(details, null, 2)}</pre>
        </details>
      )}
      {onRetry && (
        <button type="button" onClick={onRetry}>
          Retry
        </button>
      )}
    </div>
  );
}

/** Problem details (RFC 9457) for a failed action, e.g. a rejected scenario. */
export function ProblemView({ error }: { error: unknown }) {
  const api = error instanceof ApiError ? error : null;
  if (!api) return <p role="alert">{error instanceof Error ? error.message : String(error)}</p>;
  const p = api.problem;
  return (
    <div className="problem" role="alert">
      <p>
        <strong>{p.title ?? 'Request rejected'}</strong>
        {api.status ? ` (HTTP ${api.status}${api.code ? `, ${api.code}` : ''})` : ''}
      </p>
      {p.detail && <p>{p.detail}</p>}
      {p.details && (
        <details open>
          <summary>Details</summary>
          <pre className="json">{JSON.stringify(p.details, null, 2)}</pre>
        </details>
      )}
    </div>
  );
}
