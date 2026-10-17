import { useCallback, useEffect, useId, useState } from 'react';
import { request } from '../../api/http';
import { ApiError } from '../../api/problem';

interface Check {
  name: string;
  ok: boolean;
  detail?: string | null;
}

export type HealthState =
  | { status: 'loading' }
  | { status: 'ready' | 'not_ready'; checks: Check[]; at: Date }
  | { status: 'unreachable'; at: Date };

export type IndexState = { status: 'unknown' } | { status: 'absent' } | { status: 'missing' } | { status: 'built'; builtAt: string | null; documents: number | null };

const CHECK_FR: Record<string, string> = {
  database: 'Base de données',
  migrations: 'Migrations',
  runtime_role: 'Rôle d’exécution (lecture seule)',
  object_store: 'Stockage des artefacts',
};

export const HEALTH_POLL_MS = 60_000;

/** /health/ready (503 still carries the checks) and /v1/v8/search/status. */
export async function fetchHealth(signal?: AbortSignal): Promise<HealthState> {
  try {
    const res = await fetch('/health/ready', { credentials: 'same-origin', signal, headers: { Accept: 'application/json' } });
    const body = (await res.json().catch(() => null)) as { status?: string; checks?: Check[] } | null;
    if (!body || !Array.isArray(body.checks)) return { status: 'unreachable', at: new Date() };
    return { status: body.status === 'ready' ? 'ready' : 'not_ready', checks: body.checks, at: new Date() };
  } catch (e) {
    if (e instanceof DOMException && e.name === 'AbortError') throw e;
    return { status: 'unreachable', at: new Date() };
  }
}

export async function fetchIndex(signal?: AbortSignal): Promise<IndexState> {
  try {
    const res = await request<{ data?: { built?: boolean; built_at?: string | null; documents?: number | null } }>('GET', '/v1/v8/search/status', { signal });
    const d = res.body?.data;
    if (!d) return { status: 'unknown' };
    return d.built ? { status: 'built', builtAt: d.built_at ?? null, documents: d.documents ?? null } : { status: 'missing' };
  } catch (e) {
    if (e instanceof DOMException && e.name === 'AbortError') throw e;
    if (e instanceof ApiError && e.status === 404) return { status: 'absent' };
    if (e instanceof ApiError && e.status === 503) return { status: 'missing' };
    return { status: 'unknown' };
  }
}

const LABEL: Record<HealthState['status'], string> = {
  loading: 'Vérification…',
  ready: 'Données prêtes',
  not_ready: 'Données dégradées',
  unreachable: 'API injoignable',
};

/** "État des données" indicator in the top bar: a dot + label, details on hover/focus/click. */
export function HealthIndicator() {
  const [h, setH] = useState<HealthState>({ status: 'loading' });
  const [idx, setIdx] = useState<IndexState>({ status: 'unknown' });
  const [open, setOpen] = useState(false);
  const id = useId();
  const refresh = useCallback((signal?: AbortSignal) => {
    fetchHealth(signal).then(setH, () => undefined);
    fetchIndex(signal).then(setIdx, () => undefined);
  }, []);
  useEffect(() => {
    const c = new AbortController();
    refresh(c.signal);
    const t = setInterval(() => document.visibilityState !== 'hidden' && refresh(), HEALTH_POLL_MS);
    const onVis = () => document.visibilityState === 'visible' && refresh();
    document.addEventListener('visibilitychange', onVis);
    return () => {
      c.abort();
      clearInterval(t);
      document.removeEventListener('visibilitychange', onVis);
    };
  }, [refresh]);
  const failing = h.status === 'ready' || h.status === 'not_ready' ? h.checks.filter((c) => !c.ok) : [];
  return (
    <div className={`v8-health v8-health-${h.status}${open ? ' v8-health-open' : ''}`} data-testid="v8-health" data-status={h.status}>
      <button type="button" className="v8-top-btn v8-health-btn" aria-describedby={id} aria-expanded={open} onClick={() => setOpen((o) => !o)} onBlur={() => setOpen(false)}>
        <span className="v8-health-dot" aria-hidden="true" />
        <span className="v8-health-label">{LABEL[h.status]}</span>
        <span className="v8-sr-only"> — état des données</span>
      </button>
      <div className="v8-health-pop" role="tooltip" id={id}>
        <p className="v8-health-title">État des données</p>
        {h.status === 'unreachable' && <p>Le serveur REVA ne répond pas à /health/ready. Les valeurs affichées peuvent être anciennes.</p>}
        {(h.status === 'ready' || h.status === 'not_ready') && (
          <ul>
            {h.checks.map((c) => (
              <li key={c.name} className={c.ok ? 'ok' : 'ko'}>
                <span aria-hidden="true">{c.ok ? '●' : '○'}</span> {CHECK_FR[c.name] ?? c.name}
                {!c.ok && <span className="v8-sr-only"> (en échec)</span>}
                {c.detail && <span className="v8-health-detail"> — {c.detail}</span>}
              </li>
            ))}
          </ul>
        )}
        <p className="v8-health-index">
          Index de recherche V8 :{' '}
          {idx.status === 'built'
            ? `construit${idx.builtAt ? ` le ${idx.builtAt.slice(0, 16).replace('T', ' ')}` : ''}${idx.documents !== null ? ` · ${idx.documents.toLocaleString('fr-FR')} documents` : ''}`
            : idx.status === 'missing'
              ? 'absent (recherche de secours : graphe + catalogue atlas)'
              : idx.status === 'absent'
                ? 'service non déployé (recherche de secours)'
                : 'inconnu'}
        </p>
        {failing.length > 0 && <p className="v8-health-warn">{failing.length} vérification(s) en échec.</p>}
        {'at' in h && <p className="v8-health-at">Vérifié à {h.at.toLocaleTimeString('fr-FR', { hour: '2-digit', minute: '2-digit' })}</p>}
      </div>
    </div>
  );
}
