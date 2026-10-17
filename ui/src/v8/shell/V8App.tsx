import { Suspense, useCallback, useEffect, useLayoutEffect, useMemo, useState } from 'react';
import { Route, Routes, useLocation, useNavigate } from 'react-router';
import type { PaletteProvider } from '../contract';
import { Breadcrumbs, setShellTitle, TITLE_SUFFIX } from './Breadcrumbs';
import { FeatureBoundary } from './Boundary';
import { featureForPath, navFeatures, type DiscoveredFeature } from './discovery';
import { HealthIndicator } from './Health';
import { createKeyHandler } from './keys';
import { NavSheet, Rail } from './Nav';
import { defaultSearch, onSearchBackend, searchBackend, type SearchBackend } from './palette/defaultProvider';
import type { BuiltinAction } from './palette/engine';
import { Palette } from './palette/Palette';
import { clearRecents } from './palette/recents';
import { BuiltinHome, NotFound } from './Placeholder';
import { prefetchElement } from './prefetch';
import { ShortcutsSheet } from './ShortcutsSheet';
import { THEME_LABEL, toggledPref, useTheme } from './theme';
import { KeyHint } from './ui/KeyHint';
import { Skeleton } from './ui/States';
// Styles only (not the primitives barrel): the shell chunk stays free of zod / react-table / react-virtual.
import './tokens.css';
import './ui/primitives.css';
import './shell.css';

const RAIL_KEY = 'v8.rail.collapsed';

function readCollapsed(): boolean {
  try {
    return localStorage.getItem(RAIL_KEY) === '1';
  } catch {
    return false;
  }
}

function RouteFallback() {
  return (
    <div className="v8-page" aria-busy="true">
      <Skeleton height={28} width={220} />
      <Skeleton height={320} />
    </div>
  );
}

function FeatureRoute({ f }: { f: DiscoveredFeature }) {
  const { pathname } = useLocation();
  const El = f.element;
  return (
    <FeatureBoundary resetKey={pathname} title={f.title}>
      <Suspense fallback={<RouteFallback />}>
        <El />
      </Suspense>
    </FeatureBoundary>
  );
}

export interface V8AppProps {
  features: DiscoveredFeature[];
  problems?: string[];
  username?: string | null;
  onLogout?: () => void;
  /** Override the default search provider (tests). */
  defaultProvider?: PaletteProvider | null;
}

/** The V8 console frame: rail, top bar (breadcrumbs, palette, data status, theme), routes of every feature. */
export function V8App({ features, problems = [], username, onLogout, defaultProvider = defaultSearch }: V8AppProps) {
  const navigate = useNavigate();
  const { pathname } = useLocation();
  const nav = useMemo(() => navFeatures(features), [features]);
  const [paletteOpen, setPaletteOpen] = useState(false);
  const [helpOpen, setHelpOpen] = useState(false);
  const [sheetOpen, setSheetOpen] = useState(false);
  const [collapsed, setCollapsed] = useState(readCollapsed);
  const [backend, setBackend] = useState<SearchBackend>(searchBackend());
  const { pref, theme, setPref } = useTheme();

  useEffect(() => onSearchBackend(setBackend), []);

  // The console owns the page while mounted (background, color-scheme); old screens are untouched after.
  useEffect(() => {
    document.documentElement.classList.add('v8-active');
    return () => document.documentElement.classList.remove('v8-active');
  }, []);

  // Default title per route (layout effect: a feature's own useEffect title wins).
  const current = featureForPath(features, pathname);
  useLayoutEffect(() => {
    setShellTitle(current ? `${current.title} · ${TITLE_SUFFIX}` : `Console · ${TITLE_SUFFIX}`);
  }, [pathname, current]);
  useEffect(() => {
    window.scrollTo?.(0, 0);
    setSheetOpen(false);
  }, [pathname]);

  const toggleRail = useCallback(() => {
    setCollapsed((c) => {
      try {
        localStorage.setItem(RAIL_KEY, c ? '0' : '1');
      } catch {
        /* per page view */
      }
      return !c;
    });
  }, []);

  const go = useCallback(
    (key: string) => {
      const f = features.find((x) => x.goKey === key && x.navTo);
      if (!f) return false;
      navigate(f.navTo!);
      return true;
    },
    [features, navigate],
  );

  useEffect(() => {
    const handler = createKeyHandler({
      openPalette: () => {
        setHelpOpen(false);
        setPaletteOpen((o) => !o);
      },
      openHelp: () => setHelpOpen((o) => !o),
      go,
      toggleRail,
    });
    window.addEventListener('keydown', handler);
    return () => window.removeEventListener('keydown', handler);
  }, [go, toggleRail]);

  const providers = useMemo<PaletteProvider[]>(() => [...(defaultProvider ? [defaultProvider] : []), ...features.flatMap((f) => (f.search ? [f.search] : []))], [features, defaultProvider]);

  const builtins = useMemo<BuiltinAction[]>(
    () => [
      { id: 'theme-toggle', label: `Thème : passer en ${theme === 'dark' ? 'clair' : 'sombre'}`, keywords: ['thème', 'theme', 'sombre', 'clair', 'dark', 'light'], run: () => setPref(toggledPref(pref)) },
      { id: 'theme-system', label: 'Thème : suivre le système', keywords: ['thème', 'système', 'auto'], run: () => setPref('system') },
      { id: 'rail', label: collapsed ? 'Déplier la navigation' : 'Replier la navigation', keywords: ['navigation', 'menu', 'rail'], run: toggleRail, hint: '[' },
      { id: 'help', label: 'Raccourcis clavier', keywords: ['aide', 'raccourcis', 'clavier', 'help'], run: () => setHelpOpen(true), hint: '?' },
      { id: 'recents', label: 'Effacer les éléments récents', keywords: ['récents', 'historique'], run: clearRecents },
      { id: 'old', label: 'Anciens écrans (V1–V5)', keywords: ['ancien', 'v3', 'planète', 'retour'], run: () => (window.location.href = '/') },
    ],
    [theme, pref, setPref, collapsed, toggleRail],
  );

  const onActive = useCallback(
    (href: string | null) => {
      if (!href) return;
      const f = featureForPath(features, href.split('?')[0] ?? href);
      if (f) prefetchElement(f.element);
    },
    [features],
  );

  const index = features.find((f) => f.route === '');
  return (
    <div className={`v8-shell${collapsed ? ' v8-shell-collapsed' : ''}`} data-theme-effective={theme}>
      <a className="v8-skip" href="#v8-main">
        Aller au contenu
      </a>
      <Rail features={nav} collapsed={collapsed} onToggle={toggleRail} username={username} onLogout={onLogout} />
      <div className="v8-body">
        <header className="v8-top">
          <button type="button" className="v8-top-btn v8-menu-btn" onClick={() => setSheetOpen(true)} aria-label="Ouvrir la navigation" aria-haspopup="dialog">
            <svg viewBox="0 0 16 16" width="16" height="16" aria-hidden="true">
              <path d="M2.5 4h11M2.5 8h11M2.5 12h11" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" />
            </svg>
          </button>
          <Breadcrumbs features={features} />
          <div className="v8-top-right">
            <button type="button" className="v8-search-trigger" onClick={() => setPaletteOpen(true)} aria-haspopup="dialog" aria-keyshortcuts="Control+K Meta+K /">
              <svg viewBox="0 0 16 16" width="14" height="14" aria-hidden="true">
                <circle cx="7" cy="7" r="4.5" fill="none" stroke="currentColor" strokeWidth="1.5" />
                <path d="M10.5 10.5 14 14" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" />
              </svg>
              <span className="v8-search-text">Rechercher…</span>
              <KeyHint keys="Mod K" />
            </button>
            <HealthIndicator />
            <button
              type="button"
              className="v8-top-btn"
              onClick={() => setPref(toggledPref(pref))}
              aria-label={`Thème ${THEME_LABEL[pref]} — passer en ${theme === 'dark' ? 'clair' : 'sombre'}`}
              title={`Thème : ${THEME_LABEL[pref]}${pref === 'system' ? ` (${theme === 'dark' ? 'sombre' : 'clair'})` : ''}`}
              data-testid="v8-theme-toggle"
            >
              {theme === 'dark' ? (
                <svg viewBox="0 0 16 16" width="16" height="16" aria-hidden="true">
                  <circle cx="8" cy="8" r="3" fill="none" stroke="currentColor" strokeWidth="1.4" />
                  <path d="M8 1.5v1.6M8 12.9v1.6M1.5 8h1.6M12.9 8h1.6M3.4 3.4l1.1 1.1M11.5 11.5l1.1 1.1M3.4 12.6l1.1-1.1M11.5 4.5l1.1-1.1" stroke="currentColor" strokeWidth="1.4" strokeLinecap="round" />
                </svg>
              ) : (
                <svg viewBox="0 0 16 16" width="16" height="16" aria-hidden="true">
                  <path d="M13.5 9.6A5.6 5.6 0 0 1 6.4 2.5a5.6 5.6 0 1 0 7.1 7.1Z" fill="none" stroke="currentColor" strokeWidth="1.4" strokeLinejoin="round" />
                </svg>
              )}
            </button>
            <button type="button" className="v8-top-btn v8-help-btn" onClick={() => setHelpOpen(true)} aria-label="Raccourcis clavier" title="Raccourcis clavier ( ? )">
              ?
            </button>
          </div>
        </header>
        <main id="v8-main" className="v8-main" tabIndex={-1}>
          <Routes>
            {index ? <Route index element={<FeatureRoute f={index} />} /> : <Route index element={<BuiltinHome features={nav} onOpenPalette={() => setPaletteOpen(true)} />} />}
            {features
              .filter((f) => f.route !== '')
              .map((f) => (
                <Route key={f.id} path={f.route.includes('*') ? f.route : `${f.route}/*`} element={<FeatureRoute f={f} />} />
              ))}
            {/* A parameterised feature (fiche/:kind/:id) also answers its nav target (/v8/fiche). */}
            {features
              .filter((f) => f.route.includes(':') && f.navTo && f.navTo !== '/v8' && !features.some((g) => g.route === f.navTo!.slice(4)))
              .map((f) => (
                <Route key={`${f.id}-nav`} path={f.navTo!.slice(4)} element={<FeatureRoute f={f} />} />
              ))}
            <Route path="*" element={<NotFound features={nav} pathname={pathname} />} />
          </Routes>
        </main>
      </div>
      <NavSheet open={sheetOpen} onClose={() => setSheetOpen(false)} features={nav} username={username} onLogout={onLogout} />
      <Palette
        open={paletteOpen}
        onClose={() => setPaletteOpen(false)}
        features={features}
        providers={providers}
        builtins={builtins}
        navigate={(href) => navigate(href)}
        openNewTab={(href) => window.open(href, '_blank', 'noopener')}
        onActive={onActive}
        footnote={backend === 'fallback' ? 'recherche de secours (graphe + catalogue atlas)' : backend === 'v8' ? 'index V8' : undefined}
      />
      <ShortcutsSheet open={helpOpen} onClose={() => setHelpOpen(false)} features={features} problems={problems} />
    </div>
  );
}
