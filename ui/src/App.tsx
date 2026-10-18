import { lazy, Suspense, useCallback, useEffect, useState } from 'react';
import { Navigate, NavLink, Route, Routes, useLocation } from 'react-router';
import { getSession, logout } from './api/client';
import { onUnauthorized } from './api/http';
import { ApiError } from './api/problem';
import type { SessionInfo } from './api/types';
import { CommandPalette } from './components/CommandPalette';
import { ErrorBoundary } from './components/ErrorBoundary';
import { ProvenanceProvider } from './components/ProvenanceDrawer';
import { ErrorState, Loading } from './components/StateViews';
import { LoginPage } from './pages/LoginPage';
import { V3_NAV, V3_PALETTE } from './v3/screens';

const WorldPage = lazy(() => import('./pages/WorldPage'));
const CountryPage = lazy(() => import('./pages/CountryPage'));
const EvidencePage = lazy(() => import('./pages/EvidencePage'));
const GraphPage = lazy(() => import('./pages/GraphPage'));
const SimulationPage = lazy(() => import('./pages/SimulationPage'));
const ModelsPage = lazy(() => import('./pages/ModelsPage'));
const AcquisitionPage = lazy(() => import('./pages/AcquisitionPage'));
const MapPage = lazy(() => import('./pages/MapPage'));
const ComposerPage = lazy(() => import('./pages/ComposerPage'));
const RunsPage = lazy(() => import('./pages/RunsPage'));
const RunPage = lazy(() => import('./pages/RunPage'));
const V3App = lazy(() => import('./v3/V3App'));
// V8 console: its own frame (no V1 header), own chunk; features are discovered under src/v8/features/.
const loadV8 = () => import('./v8/shell/V8Root');
const V8Root = lazy(loadV8);

const isV8 = (pathname: string) => pathname === '/v8' || pathname.startsWith('/v8/');
/** V8QA sign-off: `/` (and `/login` once signed in) open the V8 console; the old screens keep their paths. */
const toV8 = (pathname: string) => pathname === '/' || pathname === '' || pathname === '/login';
// Landing on /v8 (or on / which redirects there): fetch the console chunk in parallel with the session check.
if (typeof window !== 'undefined' && (isV8(window.location.pathname) || toV8(window.location.pathname))) void loadV8().catch(() => undefined);

const NAV = [
  { to: '/world', label: 'World' },
  { to: '/map', label: 'Map' },
  { to: '/country/FRA', label: 'Country' },
  { to: '/evidence', label: 'Evidence' },
  { to: '/graph', label: 'Graph' },
  { to: '/compose', label: 'Compose' },
  { to: '/simulation', label: 'Simulation' },
  { to: '/runs', label: 'Runs' },
  { to: '/models', label: 'Models' },
  { to: '/acquisition', label: 'Acquisition' },
  V3_NAV,
];

type SessionState = { status: 'checking' } | { status: 'anonymous' } | { status: 'error'; error: unknown } | { status: 'ok'; session: SessionInfo };

export function App() {
  const [state, setState] = useState<SessionState>({ status: 'checking' });
  const location = useLocation();

  const check = useCallback(() => {
    setState({ status: 'checking' });
    getSession()
      .then((session) => setState({ status: 'ok', session }))
      .catch((error: unknown) => {
        if (error instanceof ApiError && error.kind === 'unauthenticated') setState({ status: 'anonymous' });
        else setState({ status: 'error', error });
      });
  }, []);

  useEffect(() => {
    check();
    return onUnauthorized(() => setState({ status: 'anonymous' }));
  }, [check]);

  useEffect(() => {
    if (isV8(location.pathname)) return; // the V8 shell names its own pages
    const page = NAV.find((n) => location.pathname.startsWith(n.to.split('/').slice(0, 2).join('/')));
    document.title = page ? `${page.label} · REVA World` : 'REVA World';
  }, [location.pathname]);

  if (state.status === 'checking') return isV8(location.pathname) || toV8(location.pathname) ? <div className="v8-boot" aria-busy="true" /> : <Loading what="session" />;
  if (state.status === 'error') {
    return (
      <main id="main">
        <h1>REVA World</h1>
        <ErrorState error={state.error} what="The session" onRetry={check} />
      </main>
    );
  }
  if (state.status === 'anonymous') {
    return <LoginPage onLogin={(session) => setState({ status: 'ok', session })} />;
  }

  const doLogout = async () => {
    try {
      await logout();
    } finally {
      setState({ status: 'anonymous' });
    }
  };

  if (toV8(location.pathname)) return <Navigate to={`/v8${location.search}${location.hash}`} replace />;

  if (isV8(location.pathname)) {
    return (
      <ProvenanceProvider>
        <Suspense fallback={<div className="v8-boot" aria-busy="true" />}>
          <Routes>
            <Route path="/v8/*" element={<V8Root username={state.session.username} onLogout={doLogout} />} />
          </Routes>
        </Suspense>
      </ProvenanceProvider>
    );
  }

  return (
    <ProvenanceProvider>
      <a className="skip-link" href="#main">
        Skip to content
      </a>
      <header className="app-header">
        <p className="brand">REVA World</p>
        <nav className="app-nav" aria-label="Screens">
          <ul>
            {NAV.map((n) => (
              <li key={n.to}>
                <NavLink
                  to={n.to}
                  className={({ isActive }) => (isActive ? 'active' : undefined)}
                  end={false}
                  aria-current={location.pathname.startsWith(n.to.split('/').slice(0, 2).join('/')) ? 'page' : undefined}
                >
                  {n.label}
                </NavLink>
              </li>
            ))}
          </ul>
        </nav>
        <NavLink className="v8-entry-link" to="/v8" title="La nouvelle console : recherche Ctrl-K, fiches, carte, scores…">
          Nouvelle console V8 →
        </NavLink>
        <CommandPalette screens={[...NAV, ...V3_PALETTE, { to: '/v8', label: 'Nouvelle console V8' }]} />
        <div className="session">
          <span>
            Signed in as <strong>{state.session.username}</strong>
          </span>
          <button type="button" onClick={doLogout}>
            Sign out
          </button>
        </div>
      </header>
      <main id="main" tabIndex={-1}>
        <ErrorBoundary resetKey={location.pathname}>
        <Suspense fallback={<Loading what="screen" />}>
          <Routes>
            <Route path="/world" element={<WorldPage />} />
            <Route path="/country" element={<Navigate to="/country/FRA" replace />} />
            <Route path="/country/:iso3" element={<CountryPage />} />
            <Route path="/evidence" element={<EvidencePage />} />
            <Route path="/graph" element={<GraphPage />} />
            <Route path="/simulation" element={<SimulationPage />} />
            <Route path="/models" element={<ModelsPage />} />
            <Route path="/acquisition" element={<AcquisitionPage />} />
            <Route path="/map" element={<MapPage />} />
            <Route path="/compose" element={<ComposerPage />} />
            <Route path="/runs" element={<RunsPage />} />
            <Route path="/runs/:id" element={<RunPage />} />
            <Route path="/v3/*" element={<V3App />} />
            <Route path="*" element={<p>Unknown screen. Choose a screen from the navigation.</p>} />
          </Routes>
        </Suspense>
        </ErrorBoundary>
      </main>
    </ProvenanceProvider>
  );
}
