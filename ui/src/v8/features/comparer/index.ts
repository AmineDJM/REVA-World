/**
 * Comparer — N places × M indicators aligned in time (owner V8MONDE). Route /v8/comparer?s=<series>&g=FRA,DEU
 * (&t=index|yoy|percap, &vue=tableau, &vint=first|<vintage>). Palette: "comparer FRA DEU inflation".
 */
import { lazy } from 'react';
import type { V8Feature } from '../../contract';

/** Palette command → Comparer URL; the page resolves places (ISO3 or names) and the indicator words. */
export function compareHref(query: string): string | null {
  const rest = query.trim().replace(/^(comparer|compare|comparaison|cmp)\b\s*/i, '');
  return rest ? `/v8/comparer?${new URLSearchParams({ q: rest }).toString()}` : '/v8/comparer';
}

export const feature: V8Feature = {
  id: 'comparer',
  title: 'Comparer',
  path: 'comparer',
  order: 30,
  icon: '⇆',
  goKey: 'c',
  element: lazy(() => import('./ComparerPage')),
  commands: [
    {
      id: 'comparer.run',
      label: 'Comparer des pays sur un indicateur (« comparer FRA DEU inflation »)',
      keywords: ['comparer', 'compare', 'comparaison', 'vs'],
      run: (q) => compareHref(q),
    },
  ],
};
