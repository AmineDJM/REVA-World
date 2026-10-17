/**
 * Monde — space-time explorer of every atlas indicator (owner V8MONDE). Route /v8/monde; state in the URL
 * (see ./url.ts). Palette: series hits ("Séries") open the map of that series.
 */
import { lazy } from 'react';
import type { PaletteHit, V8Feature } from '../../contract';

export const feature: V8Feature = {
  id: 'monde',
  title: 'Monde',
  path: 'monde',
  order: 20,
  icon: '◍',
  goKey: 'm',
  element: lazy(() => import('./MondePage')),
  commands: [
    {
      id: 'monde.open',
      label: 'Carte du monde d’un indicateur',
      keywords: ['monde', 'carte', 'map', 'atlas', 'choroplèthe', 'indicateur'],
      run: () => '/v8/monde',
    },
  ],
  search: async (query: string, signal: AbortSignal): Promise<PaletteHit[]> => {
    const q = query.trim();
    if (q.length < 3) return [];
    const [{ getCatalog }, { mondeHref, selectorOfSeries }] = await Promise.all([import('./api'), import('./url')]);
    const r = await getCatalog(q, null, signal);
    return r.hits
      .filter((h) => h.table && h.measure !== null && h.in_snapshot)
      .slice(0, 6)
      .map((h) => ({
        id: `monde:${h.kind}:${h.id}`,
        label: h.label,
        sublabel: `carte · ${h.sublabel ?? h.table}`,
        href: mondeHref(selectorOfSeries(h.table!, h.measure!, h.filters)),
        group: 'Séries',
      }));
  },
};
