import type { CSSProperties, ReactNode } from 'react';
import { classifyV3Error } from '../../../v3/api/common';

/**
 * Empty state: say WHAT is missing and WHY (never invent demo numbers). `action` is a button or link.
 */
export function EmptyState({ title, children, action, icon = '∅', compact = false }: { title: ReactNode; children?: ReactNode; action?: ReactNode; icon?: ReactNode; compact?: boolean }) {
  return (
    <div className={`v8-empty${compact ? ' v8-empty-compact' : ''}`} role="status">
      <span className="v8-empty-icon" aria-hidden="true">
        {icon}
      </span>
      <div className="v8-empty-body">
        <p className="v8-empty-title">{title}</p>
        {children && <div className="v8-empty-text">{children}</div>}
        {action && <div className="v8-empty-action">{action}</div>}
      </div>
    </div>
  );
}

const SOFT_HINT: Record<string, string> = {
  unavailable: 'Ce service n’est pas encore disponible sur ce serveur. Rien n’est simulé à la place.',
  no_data: 'Le service existe mais rien n’est chargé : c’est une absence de données, pas une valeur nulle.',
  not_found: 'L’élément demandé est inconnu des données chargées.',
  unreachable: 'Le serveur REVA ne répond pas.',
  rate_limited: 'Réessayez dans un instant.',
  auth: 'Votre session n’a pas accès à ce service.',
};

/**
 * Error state from any thrown error (ApiError / ContractError / Error), classified like V3:
 * 404 route → "Pas encore disponible", no snapshot → "Aucune donnée chargée", unknown id → "Introuvable".
 */
export function ErrorState({ error, what, onRetry, children }: { error: unknown; what: string; onRetry?: () => void; children?: ReactNode }) {
  const info = classifyV3Error(error);
  const soft = info.kind === 'unavailable' || info.kind === 'no_data' || info.kind === 'not_found';
  return (
    <div className={`v8-error${soft ? ' v8-error-soft' : ''}`} role={soft ? 'status' : 'alert'} data-v8-error={info.kind}>
      <p className="v8-error-title">
        <strong>{info.title}</strong> — {what}
        {info.status ? <span className="v8-error-code"> HTTP {info.status}{info.code ? ` · ${info.code}` : ''}</span> : null}
      </p>
      {SOFT_HINT[info.kind] && <p className="v8-error-text">{SOFT_HINT[info.kind]}</p>}
      {info.detail && info.kind !== 'unavailable' && <p className="v8-error-detail">{info.detail}</p>}
      {info.issues && info.issues.length > 0 && (
        <details className="v8-error-issues">
          <summary>Écarts au contrat ({info.issues.length})</summary>
          <ul>
            {info.issues.map((i) => (
              <li key={i}>
                <code>{i}</code>
              </li>
            ))}
          </ul>
        </details>
      )}
      {children}
      {onRetry && (
        <button type="button" className="v8-btn v8-btn-sm" onClick={onRetry}>
          Réessayer
        </button>
      )}
    </div>
  );
}

/**
 * Loading placeholder with a FIXED footprint (no layout shift): give it the height of the content it stands for.
 */
export function Skeleton({ lines = 3, height, width, label = 'Chargement…', style }: { lines?: number; height?: number | string; width?: number | string; label?: string; style?: CSSProperties }) {
  if (height !== undefined) {
    return <div className="v8-skel v8-skel-block" style={{ height, width, ...style }} role="status" aria-label={label} />;
  }
  return (
    <div className="v8-skel-lines" role="status" aria-label={label} style={{ width, ...style }}>
      {Array.from({ length: lines }, (_, i) => (
        <span key={i} className="v8-skel v8-skel-line" style={{ width: `${i === lines - 1 && lines > 1 ? 62 : 100}%` }} />
      ))}
    </div>
  );
}
