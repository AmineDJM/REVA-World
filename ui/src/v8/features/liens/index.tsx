/**
 * Liens — how anything connects to anything (owner V8LIENS). Route /v8/liens?from=&to=&fam=&causal=1&hops=&p=&n=
 * (v8Links.path(from, to) builds the base URL). API: /v1/v8/paths, /v1/v8/neighbourhood, /v1/v8/paths/nodes.
 */
import { lazy } from 'react';
import type { V8Feature } from '../../contract';
import { v8Links } from '../../contract';

const ID = /^(Q[1-9]\d{0,11}|[A-Z]{3}|[a-z][a-z0-9_]{1,31}:[A-Za-z0-9_.:-]{1,160}(@[A-Z]{3})?)$/;

export const feature: V8Feature = {
  id: 'liens',
  title: 'Liens',
  path: 'liens',
  order: 40,
  icon: '⟿',
  goKey: 'l',
  element: lazy(() => import('./LiensPage')),
  commands: [
    {
      id: 'liens.open',
      label: 'Liens : comment deux choses sont-elles reliées ?',
      keywords: ['liens', 'chemin', 'relié', 'connexion', 'path', 'how is', 'lien entre'],
      run: (query: string) => {
        // "liens Q851 Q142" / "liens FRA DEU" → the path between two ids typed in the palette
        const ids = query.split(/\s+/).filter((t) => ID.test(t));
        if (ids.length >= 2) return v8Links.path(ids[0]!, ids[1]!);
        return '/v8/liens';
      },
    },
  ],
};
