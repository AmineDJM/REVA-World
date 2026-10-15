import type { ReactNode } from 'react';
import { classifyV3Error } from '../api/common';

export function V3Loading({ what }: { what: string }) {
  return (
    <p className="state state-loading v3-loading" role="status" aria-live="polite">
      <span className="v3-spinner" aria-hidden="true" />
      Chargement {what}…
    </p>
  );
}

const HINT: Record<string, string> = {
  unavailable:
    'Ce service du serveur n’est pas encore disponible (point d’accès absent ou non implémenté). Rien n’est affiché à la place : aucune donnée n’est simulée.',
  no_data: 'Le service existe mais aucune donnée n’a encore été chargée. Ce n’est pas une valeur nulle : c’est une absence de données.',
  not_found: 'L’élément demandé est inconnu des données chargées.',
  invalid: 'Le serveur a refusé la requête.',
  rate_limited: 'Réessayez dans un instant.',
  unreachable: 'Le serveur REVA ne répond pas.',
  contract: '',
  auth: 'Votre session n’a pas accès à ce service.',
  error: '',
};

/** Error/absence state for V3 screens: "pas encore disponible", "aucune donnée", "introuvable"... */
export function V3Error({ error, what, onRetry, children }: { error: unknown; what: string; onRetry?: () => void; children?: ReactNode }) {
  const info = classifyV3Error(error);
  const soft = info.kind === 'unavailable' || info.kind === 'no_data' || info.kind === 'not_found';
  return (
    <div className={`v3-state v3-state-${info.kind} ${soft ? 'state state-empty' : 'state state-error'}`} role={soft ? 'status' : 'alert'} data-v3-state={info.kind}>
      <p>
        <strong>{info.title}</strong> — {what}
        {info.status ? ` (HTTP ${info.status}${info.code ? `, ${info.code}` : ''})` : ''}.
      </p>
      {HINT[info.kind] && <p>{HINT[info.kind]}</p>}
      {info.detail && info.kind !== 'unavailable' && <p className="muted">{info.detail}</p>}
      {info.issues && info.issues.length > 0 && (
        <details>
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
        <button type="button" onClick={onRetry}>
          Réessayer
        </button>
      )}
    </div>
  );
}

export function V3Empty({ children }: { children: ReactNode }) {
  return (
    <div className="v3-state state state-empty" role="status">
      {children}
    </div>
  );
}
