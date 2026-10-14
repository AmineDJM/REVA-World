import { lazy, Suspense, useCallback, useEffect, useState } from 'react';
import { Navigate, NavLink, Route, Routes, useLocation } from 'react-router';
import { getSession, logout } from './api/client';
import { onUnauthorized } from './api/http';
import { ApiError } from './api/problem';
import type { SessionInfo } from './api/types';
import { ErrorBoundary } from './components/ErrorBoundary';
import { ProvenanceProvider } from './components/ProvenanceDrawer';
import { ErrorState, Loading } from './components/StateViews';
import { LoginPage } from './pages/LoginPage';

const WorldPage = lazy(() => import('./pages/WorldPage'));
const CountryPage = lazy(() => import('./pages/CountryPage'));
const EvidencePage = lazy(() => import('./pages/EvidencePage'));
const GraphPage = lazy(() => import('./pages/GraphPage'));
const SimulationPage = lazy(() => import('./pages/SimulationPage'));
const ModelsPage = lazy(() => import('./pages/ModelsPage'));
const AcquisitionPage = lazy(() => import('./pages/AcquisitionPage'));

const NAV = [
  { to: '/world', label: 'World' },
  { to: '/country/FRA', label: 'Country' },
  { to: '/evidence', label: 'Evidence' },
  { to: '/graph', label: 'Graph' },
  { to: '/simulation', label: 'Simulation' },
  { to: '/models', label: 'Models' },
  { to: '/acquisition', label: 'Acquisition' },
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
    const page = NAV.find((n) => location.pathname.startsWith(n.to.split('/').slice(0, 2).join('/')));
    document.title = page ? `${page.label} · REVA World` : 'REVA World';
  }, [location.pathname]);

  if (state.status === 'checking') return <Loading what="session" />;
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
            <Route path="/" element={<Navigate to="/world" replace />} />
            <Route path="/world" element={<WorldPage />} />
            <Route path="/country" element={<Navigate to="/country/FRA" replace />} />
            <Route path="/country/:iso3" element={<CountryPage />} />
            <Route path="/evidence" element={<EvidencePage />} />
            <Route path="/graph" element={<GraphPage />} />
            <Route path="/simulation" element={<SimulationPage />} />
            <Route path="/models" element={<ModelsPage />} />
            <Route path="/acquisition" element={<AcquisitionPage />} />
            <Route path="/login" element={<Navigate to="/world" replace />} />
            <Route path="*" element={<p>Unknown screen. Choose a screen from the navigation.</p>} />
          </Routes>
        </Suspense>
        </ErrorBoundary>
      </main>
    </ProvenanceProvider>
  );
}
