/**
 * Accueil — « le pouls du monde » (owner V8BOARDS): the /v8 index. What changed (change feed), what needs the
 * owner (decisions), what resolves next (calendar), pinned boards, coverage at a glance, quick actions.
 */
import { lazy } from 'react';
import type { V8Feature } from '../../contract';

export const feature: V8Feature = {
  id: 'accueil',
  title: 'Accueil',
  path: '',
  order: 0,
  icon: '⌂',
  goKey: 'h',
  element: lazy(() => import('./AccueilPage')),
  commands: [
    { id: 'accueil.changes', label: 'Ce qui a changé aujourd’hui', keywords: ['changements', 'nouveautés', 'pouls', 'accueil', 'aujourd’hui'], run: () => '/v8' },
    { id: 'accueil.changes.week', label: 'Ce qui a changé cette semaine', keywords: ['changements', 'semaine', 'nouveautés'], run: () => '/v8?p=7j' },
    { id: 'accueil.resolved', label: 'Questions résolues récemment', keywords: ['résolutions', 'résolues', 'verdict', 'prévisions'], run: () => '/v8?p=30j&k=question_resolved' },
  ],
};
