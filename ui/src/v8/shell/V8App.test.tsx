import { act, fireEvent, render, screen } from '@testing-library/react';
import { lazy } from 'react';
import { MemoryRouter, Route, Routes, useLocation, useParams } from 'react-router';
import { collectFeatures } from './discovery';
import { V8App } from './V8App';

function Where() {
  const l = useLocation();
  return <output data-testid="where">{l.pathname + l.search}</output>;
}

const Monde = () => <h1>Page Monde</h1>;
const Lazy = lazy(async () => ({ default: () => <h1>Page Scores</h1> }));
const Boom = () => {
  throw new Error('kaput');
};
const { features, problems } = collectFeatures({
  '../features/monde/index.tsx': { feature: { id: 'monde', title: 'Monde', path: 'monde', order: 20, goKey: 'm', icon: '◍', element: Monde } },
  '../features/scores/index.tsx': { feature: { id: 'scores', title: 'Scores', path: 'scores', order: 30, goKey: 's', element: Lazy } },
  '../features/fiche/index.tsx': { feature: { id: 'fiche', title: 'Fiche', path: 'fiche/:kind/:id', navPath: 'fiche', order: 10, element: () => <h1>Page Fiche</h1> } },
  '../features/boom/index.tsx': { feature: { id: 'boom', title: 'Boom', path: 'boom', order: 90, element: Boom } },
});

function renderAt(url: string) {
  globalThis.fetch = vi.fn(async () => new Response(JSON.stringify({ status: 'ready', checks: [{ name: 'database', ok: true }] }), { status: 200 })) as typeof fetch;
  return render(
    <MemoryRouter initialEntries={[url]}>
      <Routes>
        <Route path="/v8/*" element={<V8App features={features} problems={problems} username="admin" defaultProvider={null} />} />
        <Route path="*" element={<p>old</p>} />
      </Routes>
      <Where />
    </MemoryRouter>,
  );
}

describe('V8App shell', () => {
  beforeEach(() => {
    localStorage.clear();
    window.matchMedia = ((q: string) => ({ matches: false, media: q, addEventListener: () => undefined, removeEventListener: () => undefined })) as unknown as typeof window.matchMedia;
  });

  it('lists discovered features in the nav (order) and routes to them', async () => {
    renderAt('/v8/monde');
    const nav = screen.getByRole('navigation', { name: 'Sections' });
    const links = Array.from(nav.querySelectorAll('.v8-nav-text')).map((a) => a.textContent);
    expect(links).toEqual(['Fiche', 'Monde', 'Scores', 'Boom']);
    expect(screen.getByRole('heading', { name: 'Page Monde' })).toBeInTheDocument();
    expect(document.documentElement.classList.contains('v8-active')).toBe(true);
  });

  it('g <key> navigates; lazy feature resolves; breadcrumbs follow the URL', async () => {
    renderAt('/v8');
    await act(async () => {
      fireEvent.keyDown(window, { key: 'g' });
      fireEvent.keyDown(window, { key: 's' });
    });
    expect(screen.getByTestId('where')).toHaveTextContent('/v8/scores');
    expect(await screen.findByRole('heading', { name: 'Page Scores' })).toBeInTheDocument();
    const crumbs = screen.getByRole('navigation', { name: 'Fil d’Ariane' });
    expect(crumbs).toHaveTextContent('Console');
    expect(crumbs).toHaveTextContent('Scores');
  });

  it('parameterised feature answers its route and its nav target', () => {
    renderAt('/v8/fiche/country/FRA');
    expect(screen.getByRole('heading', { name: 'Page Fiche' })).toBeInTheDocument();
    expect(screen.getByRole('navigation', { name: 'Fil d’Ariane' })).toHaveTextContent('FRA');
  });

  it('a deep navPath (/v8/x/country/FRA) does not shadow the param route: the feature gets its params', () => {
    const Params = () => {
      const p = useParams();
      return <h1>{`params ${p.kind ?? '∅'}/${p.id ?? '∅'}`}</h1>;
    };
    const d = collectFeatures({
      '../features/deep/index.tsx': { feature: { id: 'deep', title: 'Deep', path: 'deep/:kind/:id', navPath: '/v8/deep/country/FRA', order: 10, element: Params } },
    });
    globalThis.fetch = vi.fn(async () => new Response('{}', { status: 200 })) as typeof fetch;
    render(
      <MemoryRouter initialEntries={['/v8/deep/country/FRA']}>
        <Routes>
          <Route path="/v8/*" element={<V8App features={d.features} problems={d.problems} username="admin" defaultProvider={null} />} />
        </Routes>
      </MemoryRouter>,
    );
    expect(screen.getByRole('heading', { name: 'params country/FRA' })).toBeInTheDocument();
  });

  it('a crashing feature keeps the frame and names the failure', () => {
    const err = vi.spyOn(console, 'error').mockImplementation(() => undefined);
    renderAt('/v8/boom');
    expect(screen.getByRole('alert')).toHaveTextContent('Cette section a échoué');
    expect(screen.getByRole('navigation', { name: 'Sections' })).toBeInTheDocument();
    err.mockRestore();
  });

  it('unknown URL → placeholder, no invented content', () => {
    renderAt('/v8/nexistepas');
    expect(screen.getByText(/ne correspond à aucune section/)).toBeInTheDocument();
  });

  it('built-in home when no feature claims /v8; Ctrl-K and ? open palette and help; theme toggles', async () => {
    renderAt('/v8');
    expect(screen.getByRole('heading', { name: 'Console REVA' })).toBeInTheDocument();
    fireEvent.keyDown(window, { key: 'k', ctrlKey: true });
    expect(screen.getByRole('dialog', { name: /Palette/ })).toBeInTheDocument();
    fireEvent.keyDown(screen.getByRole('combobox'), { key: 'Escape' });
    expect(screen.queryByRole('dialog', { name: /Palette/ })).toBeNull();
    fireEvent.keyDown(window, { key: '?' });
    expect(screen.getByRole('dialog', { name: 'Raccourcis clavier' })).toHaveTextContent('Monde');
    fireEvent.click(screen.getByTestId('v8-theme-toggle'));
    expect(document.documentElement.dataset.theme).toBe('dark');
  });

  it('health indicator reads /health/ready', async () => {
    renderAt('/v8');
    expect(await screen.findByText('Données prêtes')).toBeInTheDocument();
  });
});

describe('breadcrumb page label', () => {
  it('uses the feature title of the page, never the shell default', async () => {
    globalThis.fetch = vi.fn(async () => new Response('{}', { status: 200 })) as typeof fetch;
    render(
      <MemoryRouter initialEntries={['/v8/nexiste/pas/FRA']}>
        <Routes>
          <Route path="/v8/*" element={<V8App features={features} defaultProvider={null} />} />
        </Routes>
      </MemoryRouter>,
    );
    const crumbs = screen.getByRole('navigation', { name: 'Fil d’Ariane' });
    expect(crumbs).toHaveTextContent('FRA');
    expect(crumbs.querySelector('[aria-current=page]')).toHaveTextContent('FRA');
    await act(async () => {
      document.title = 'France · Fiche';
      await new Promise((r) => setTimeout(r, 10));
    });
    expect(crumbs.querySelector('[aria-current=page]')).toHaveTextContent('France');
  });
});
