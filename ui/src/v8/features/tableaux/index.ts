/**
 * Tableaux — personal boards of pinned items with watch rules and alerts (owner V8BOARDS).
 * Routes: /v8/tableaux, /v8/tableaux/:id, /v8/tableaux/partage#b=… (path "tableaux"; the shell adds /*).
 * Public cross-feature API: ./pin.ts (pinItem) — see docs/dev/v8_v8boards.md.
 */
import { lazy } from 'react';
import type { PaletteHit, V8Feature } from '../../contract';
import { v8Links } from '../../contract';

const norm = (s: string) =>
  s
    .toLowerCase()
    .normalize('NFD')
    .replace(/[̀-ͯ]/g, '');

export const feature: V8Feature = {
  id: 'tableaux',
  title: 'Tableaux',
  path: 'tableaux', // the shell mounts tableaux/* : /v8/tableaux, /v8/tableaux/:id, /v8/tableaux/partage#b=…
  order: 70,
  icon: '▦',
  goKey: 't',
  element: lazy(() => import('./TableauxPage')),
  commands: [
    {
      id: 'tableaux.open',
      label: 'Ouvrir mes tableaux',
      keywords: ['tableaux', 'boards', 'épingles', 'veille', 'alertes', 'watchlist'],
      run: () => '/v8/tableaux',
    },
  ],
  search: async (query: string, signal: AbortSignal): Promise<PaletteHit[]> => {
    const q = norm(query.trim());
    if (q.length < 2) return [];
    const { listBoards } = await import('./store'); // keeps the board client out of the shell bundle
    const r = await listBoards(signal);
    return r.boards
      .filter((b) => norm(b.title).includes(q))
      .slice(0, 6)
      .map((b) => ({
        id: `board:${b.id}`,
        label: b.title,
        sublabel: `Tableau · ${b.n_items} élément${b.n_items > 1 ? 's' : ''} · ${b.origin === 'server' ? 'serveur' : 'local'}`,
        href: v8Links.board(b.id),
        group: 'Tableaux',
      }));
  },
};
