import { collectFeatures, navTargetRoutes, featureForPath, navFeatures } from './discovery';

const El = () => null;
const mod = (f: Record<string, unknown>) => ({ feature: { element: El, ...f } });

describe('feature discovery', () => {
  it('orders by `order`, normalises paths, builds nav targets', () => {
    const { features, problems } = collectFeatures({
      '../features/monde/index.tsx': mod({ id: 'monde', title: 'Monde', path: '/v8/monde', order: 30, goKey: 'M' }),
      '../features/accueil/index.ts': mod({ id: 'accueil', title: 'Accueil', path: '', order: 10 }),
      '../features/fiche/index.tsx': mod({ id: 'fiche', title: 'Fiche', path: 'fiche/:kind/:id', order: 20 }),
      '../features/liens/index.tsx': mod({ id: 'liens', title: 'Liens', path: 'liens', order: 25, navHidden: true }),
    });
    expect(problems).toEqual([]);
    expect(features.map((f) => f.id)).toEqual(['accueil', 'fiche', 'liens', 'monde']);
    const byId = Object.fromEntries(features.map((f) => [f.id, f]));
    expect(byId.accueil!.navTo).toBe('/v8');
    expect(byId.monde!.route).toBe('monde');
    expect(byId.monde!.navTo).toBe('/v8/monde');
    expect(byId.monde!.goKey).toBe('m');
    expect(byId.fiche!.navTo).toBeNull(); // params and no navPath → not linkable
    expect(navFeatures(features).map((f) => f.id)).toEqual(['accueil', 'monde']);
  });

  it('reports invalid modules, duplicate routes and goKey conflicts without crashing', () => {
    const { features, problems } = collectFeatures({
      '../features/a/index.ts': mod({ id: 'a', title: 'A', path: 'x', order: 1, goKey: 'k' }),
      '../features/b/index.ts': mod({ id: 'b', title: 'B', path: 'x', order: 2 }),
      '../features/c/index.ts': mod({ id: 'c', title: 'C', path: 'c', order: 3, goKey: 'k' }),
      '../features/d/index.ts': { nothing: true },
      '../features/e/index.ts': mod({ id: 'other', title: 'E', path: 'e', order: 4 }),
    });
    expect(features.map((f) => f.id)).toEqual(['a', 'c', 'other']);
    expect(features.find((f) => f.id === 'c')!.goKey).toBeUndefined();
    expect(problems.join('\n')).toMatch(/d : le module/);
    expect(problems.join('\n')).toMatch(/route « \/v8\/x » déjà prise/);
    expect(problems.join('\n')).toMatch(/g k.*déjà pris par a/);
    expect(problems.join('\n')).toMatch(/id « other » différent du dossier/);
  });

  it('navPath makes a parameterised feature linkable; featureForPath maps URLs', () => {
    const { features } = collectFeatures({
      '../features/fiche/index.tsx': mod({ id: 'fiche', title: 'Fiche', path: 'fiche/:kind/:id', navPath: 'fiche', order: 20 }),
      '../features/scores/index.tsx': mod({ id: 'scores', title: 'Scores', path: 'scores', order: 30 }),
    });
    expect(features[0]!.navTo).toBe('/v8/fiche');
    expect(featureForPath(features, '/v8/fiche/country/FRA')?.id).toBe('fiche');
    expect(featureForPath(features, '/v8/scores')?.id).toBe('scores');
    expect(featureForPath(features, '/v8/nope')).toBeNull();
    expect(featureForPath(features, '/world')).toBeNull();
  });

  it('nav-target routes: only for visible param features whose target no route already answers', () => {
    const { features } = collectFeatures({
      '../features/fiche/index.tsx': mod({ id: 'fiche', title: 'Fiche', path: 'fiche/:kind/:id', navPath: 'fiche', order: 20 }),
      '../features/deep/index.tsx': mod({ id: 'deep', title: 'Deep', path: 'deep/:kind/:id', navPath: '/v8/deep/country/FRA', order: 21 }),
      '../features/hidden/index.tsx': mod({ id: 'hidden', title: 'Hidden', path: 'hidden/:id', navPath: 'hidden', navHidden: true, order: 22 }),
      '../features/board/index.tsx': mod({ id: 'board', title: 'Board', path: 'board/:id', navPath: 'boards', order: 23 }),
      '../features/boards/index.tsx': mod({ id: 'boards', title: 'Boards', path: 'boards', order: 24 }),
    });
    const routes = navTargetRoutes(features).map((r) => `${r.feature.id}:${r.path}`);
    // fiche gets /v8/fiche; deep's target is matched by its own param route (no shadowing static copy);
    // hidden is navHidden; board's target is another feature's route.
    expect(routes).toEqual(['fiche:fiche']);
  });
});

describe('breadcrumbs', () => {
  it('names record kinds in French and links only routable crumbs', async () => {
    const { crumbsFor } = await import('./Breadcrumbs');
    const { features } = collectFeatures({
      '../features/accueil/index.ts': mod({ id: 'accueil', title: 'Accueil', path: '', order: 1 }),
      '../features/fiche/index.tsx': mod({ id: 'fiche', title: 'Fiche', path: 'fiche/:kind/:id', navHidden: true, order: 20 }),
      '../features/dossier/index.tsx': mod({ id: 'dossier', title: 'Dossier', path: 'dossier', order: 30 }),
    });
    const c = crumbsFor('/v8/fiche/country/FRA', features, 'France');
    expect(c.map((x) => x.label)).toEqual(['Console', 'Fiche', 'pays', 'France']);
    expect(c.map((x) => x.to)).toEqual(['/v8', null, null, null]);
    const d = crumbsFor('/v8/dossier/commodity/crude_oil', features);
    expect(d.map((x) => x.label)).toEqual(['Console', 'Dossier', 'matière première', 'crude_oil']);
    expect(d[2]!.to).toBe('/v8/dossier/commodity');
  });
});
