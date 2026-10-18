import { useEffect, useRef } from 'react';
import { NavLink } from 'react-router';
import type { DiscoveredFeature } from './discovery';
import { prefetchElement } from './prefetch';
import { KeyHint } from './ui/KeyHint';

function Glyph({ f }: { f: DiscoveredFeature }) {
  return (
    <span className="v8-nav-icon" aria-hidden="true">
      {f.icon ?? f.title.slice(0, 1)}
    </span>
  );
}

function NavItems({ features, collapsed = false, onNavigate }: { features: readonly DiscoveredFeature[]; collapsed?: boolean; onNavigate?: () => void }) {
  return (
    <ul className="v8-nav-list">
      {features.map((f) => (
        <li key={f.id}>
          <NavLink
            to={f.navTo!}
            end={f.route === ''}
            className={({ isActive }) => `v8-nav-link${isActive ? ' v8-nav-active' : ''}`}
            title={collapsed ? `${f.title}${f.goKey ? ` (g ${f.goKey})` : ''}` : undefined}
            onMouseEnter={() => prefetchElement(f.element)}
            onFocus={() => prefetchElement(f.element)}
            onTouchStart={() => prefetchElement(f.element)}
            onClick={onNavigate}
          >
            <Glyph f={f} />
            <span className="v8-nav-text">{f.title}</span>
            {f.goKey && !collapsed && (
              <span className="v8-nav-key">
                <KeyHint keys={`g ${f.goKey}`} />
              </span>
            )}
          </NavLink>
        </li>
      ))}
    </ul>
  );
}

export function Rail({
  features,
  collapsed,
  onToggle,
  username,
  onLogout,
}: {
  features: readonly DiscoveredFeature[];
  collapsed: boolean;
  onToggle: () => void;
  username?: string | null;
  onLogout?: () => void;
}) {
  return (
    <aside className={`v8-rail${collapsed ? ' v8-rail-collapsed' : ''}`} aria-label="Navigation de la console">
      <div className="v8-rail-head">
        <NavLink to="/v8" className="v8-brand" title="Console REVA V8">
          <span className="v8-brand-mark" aria-hidden="true">R</span>
          <span className="v8-brand-text">
            REVA <span className="v8-brand-v">V8</span>
          </span>
        </NavLink>
        <button type="button" className="v8-rail-toggle" onClick={onToggle} aria-label={collapsed ? 'Déplier la navigation' : 'Replier la navigation'} aria-expanded={!collapsed} title={`${collapsed ? 'Déplier' : 'Replier'} ( [ )`}>
          <svg viewBox="0 0 16 16" width="14" height="14" aria-hidden="true">
            <rect x="2" y="3" width="12" height="10" rx="2" fill="none" stroke="currentColor" strokeWidth="1.3" />
            <path d="M6 3v10" stroke="currentColor" strokeWidth="1.3" />
          </svg>
        </button>
      </div>
      <nav className="v8-rail-nav" aria-label="Sections">
        {features.length > 0 ? <NavItems features={features} collapsed={collapsed} /> : <p className="v8-rail-empty">Aucune section installée.</p>}
      </nav>
      <div className="v8-rail-foot">
        <a className="v8-nav-link v8-nav-old" href="/world" title="Écrans précédents (V1–V5)">
          <span className="v8-nav-icon" aria-hidden="true">
            ↩
          </span>
          <span className="v8-nav-text">Anciens écrans</span>
        </a>
        {username && (
          <div className="v8-user">
            <span className="v8-user-name" title={`Connecté : ${username}`}>
              <span className="v8-user-avatar" aria-hidden="true">
                {username.slice(0, 1).toUpperCase()}
              </span>
              <span className="v8-nav-text">{username}</span>
            </span>
            {onLogout && (
              <button type="button" className="v8-user-out" onClick={onLogout} title="Se déconnecter">
                <span className="v8-nav-text">Déconnexion</span>
                <span className="v8-sr-only">Se déconnecter</span>
              </button>
            )}
          </div>
        )}
      </div>
    </aside>
  );
}

/** Phone: the rail becomes a bottom sheet. */
export function NavSheet({ open, onClose, features, username, onLogout }: { open: boolean; onClose: () => void; features: readonly DiscoveredFeature[]; username?: string | null; onLogout?: () => void }) {
  const ref = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (!open) return;
    const prev = document.activeElement as HTMLElement | null;
    ref.current?.querySelector<HTMLElement>('a, button')?.focus();
    return () => prev?.focus?.();
  }, [open]);
  if (!open) return null;
  return (
    <div className="v8-sheet-overlay" onMouseDown={(e) => e.target === e.currentTarget && onClose()} onKeyDown={(e) => e.key === 'Escape' && onClose()}>
      <div className="v8-sheet" role="dialog" aria-modal="true" aria-label="Navigation" ref={ref}>
        <div className="v8-sheet-grip" aria-hidden="true" />
        <NavItems features={features} onNavigate={onClose} />
        <div className="v8-sheet-foot">
          <a className="v8-nav-link" href="/world">
            <span className="v8-nav-icon" aria-hidden="true">
              ↩
            </span>
            <span className="v8-nav-text">Anciens écrans</span>
          </a>
          {username && onLogout && (
            <button type="button" className="v8-btn v8-btn-sm" onClick={onLogout}>
              Déconnexion ({username})
            </button>
          )}
        </div>
      </div>
    </div>
  );
}
