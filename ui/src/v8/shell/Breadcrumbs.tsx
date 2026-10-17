import { useEffect, useState } from 'react';
import { Link, useLocation } from 'react-router';
import { featureForPath, type DiscoveredFeature } from './discovery';

export const TITLE_SUFFIX = 'REVA V8';

let shellTitle: string | null = null;
/** The shell's default title for a route; a feature naming its page (document.title = 'France · Fiche') overrides it. */
export function setShellTitle(t: string): void {
  shellTitle = t;
  document.title = t;
}

export interface Crumb {
  label: string;
  to: string | null;
}

/** Crumbs from a /v8 URL: Console › <feature> › <segments…>; the last raw segment may be replaced by `pageLabel`. */
export function crumbsFor(pathname: string, features: readonly DiscoveredFeature[], pageLabel?: string | null): Crumb[] {
  const crumbs: Crumb[] = [{ label: 'Console', to: '/v8' }];
  const rest = pathname.replace(/^\/v8\/?/, '').replace(/\/+$/, '');
  if (!rest) return crumbs;
  const segs = rest.split('/');
  const f = featureForPath(features, pathname);
  let acc = '/v8';
  segs.forEach((s, i) => {
    acc += `/${s}`;
    let label: string;
    try {
      label = decodeURIComponent(s);
    } catch {
      label = s;
    }
    if (i === 0 && f) label = f.title;
    crumbs.push({ label, to: i === 0 && f ? (f.navTo ?? acc) : acc });
  });
  const last = crumbs[crumbs.length - 1]!;
  if (pageLabel && segs.length > 1 && pageLabel !== last.label && pageLabel !== f?.title) last.label = pageLabel;
  last.to = null;
  return crumbs;
}

/** Watches document.title; features set "France · Fiche" to name the page (first part). */
export function useTitleLabel(): string | null {
  const [t, setT] = useState(() => (typeof document !== 'undefined' ? document.title : ''));
  useEffect(() => {
    const el = document.querySelector('title');
    setT(document.title);
    if (!el || typeof MutationObserver === 'undefined') return;
    const mo = new MutationObserver(() => setT(document.title));
    mo.observe(el, { childList: true, characterData: true, subtree: true });
    return () => mo.disconnect();
  }, []);
  if (t === shellTitle) return null;
  const first = t.split(' · ')[0]?.trim() ?? '';
  return first && first !== TITLE_SUFFIX && first !== 'REVA World' ? first : null;
}

export function Breadcrumbs({ features }: { features: readonly DiscoveredFeature[] }) {
  const { pathname } = useLocation();
  const label = useTitleLabel();
  const crumbs = crumbsFor(pathname, features, label);
  return (
    <nav className="v8-crumbs" aria-label="Fil d’Ariane">
      <ol>
        {crumbs.map((c, i) => (
          <li key={`${i}-${c.label}`} className={i < crumbs.length - 2 ? 'v8-crumb-far' : undefined}>
            {c.to ? <Link to={c.to}>{c.label}</Link> : <span aria-current="page">{c.label}</span>}
          </li>
        ))}
      </ol>
    </nav>
  );
}
