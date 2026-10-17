import type { ReactNode } from 'react';
import { classifyV3Error } from '../../../v3/api/common';

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
