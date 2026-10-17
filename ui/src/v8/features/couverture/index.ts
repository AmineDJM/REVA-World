/**
 * Couverture (owner V8COUVERTURE): REVA's honest map of what it has loaded and what is missing — domain × place ×
 * period heat matrix, country profiles, freshness, queued loads and the owner's decisions (with what each unlocks).
 */
import { lazy } from 'react';
import type { PaletteHit, V8Feature } from '../../contract';
import { getDecisions, getMatrix, getPending } from './api';
import { matchItem } from './model';

const base = '/v8/couverture';
const q = (p: Record<string, string>) => `${base}?${new URLSearchParams(p).toString()}`;

export const feature: V8Feature = {
  id: 'couverture',
  title: 'Couverture',
  path: 'couverture',
  order: 80,
  icon: '▦',
  goKey: 'v',
  element: lazy(() => import('./CouverturePage')),
  commands: [
    { id: 'couverture.matrice', label: 'Couverture : domaines × continents', keywords: ['couverture', 'matrice', 'manque', 'chargé', 'lacunes', 'gaps'], run: () => base },
    { id: 'couverture.pays', label: 'Couverture : domaines × pays', keywords: ['couverture', 'pays', 'matrice'], run: () => q({ niveau: 'country' }) },
    {
      id: 'couverture.profil',
      label: 'Couverture d’un pays (code ISO3, ex. « couverture FRA »)',
      keywords: ['couverture', 'profil', 'pays'],
      run: (query) => {
        const m = /(?:^|\s)([A-Za-z]{3})$/.exec(query.trim());
        return m && !/^(pay|cou)$/i.test(m[1]!) ? q({ pays: m[1]!.toUpperCase() }) : q({ niveau: 'country' });
      },
    },
    { id: 'couverture.decisions', label: 'Décisions du propriétaire (licences, clés, autorisations)', keywords: ['décisions', 'propriétaire', 'licence', 'clé', 'compte', 'autorisation', 'owner'], run: () => q({ vue: 'decisions' }) },
    { id: 'couverture.chargements', label: 'Chargements en attente (disque, machine plus grande)', keywords: ['chargements', 'attente', 'ingestion', 'disque', 'file', 'queued'], run: () => q({ vue: 'chargements' }) },
    { id: 'couverture.fraicheur', label: 'Fraîcheur des données (dernière année, à rafraîchir)', keywords: ['fraîcheur', 'récent', 'à jour', 'retard', 'freshness'], run: () => q({ vue: 'fraicheur' }) },
  ],
  search: async (query: string, signal: AbortSignal): Promise<PaletteHit[]> => {
    const needle = query.trim();
    if (needle.length < 2) return [];
    const [m, dec, pend] = await Promise.allSettled([getMatrix('region'), getDecisions(), getPending()]);
    if (signal.aborted) return [];
    const hits: PaletteHit[] = [];
    if (m.status === 'fulfilled') {
      for (const r of m.value.rows) {
        if (matchItem(needle, [r.label, r.id])) {
          hits.push({ id: `cv-d-${r.id}`, label: `Couverture · ${r.label}`, sublabel: `${r.summary.tables} tables · ${r.summary.countries} pays`, href: q({ domaine: r.id }), group: 'Couverture' });
        }
      }
    }
    for (const [res, vue] of [
      [dec, 'decisions'],
      [pend, 'chargements'],
    ] as const) {
      if (res.status !== 'fulfilled') continue;
      for (const g of res.value.groups)
        for (const it of g.items)
          if (matchItem(needle, [it.title, it.unlocks.text, ...it.unlocks.atlas_sources])) {
            hits.push({ id: `cv-p-${it.id}`, label: it.title, sublabel: `${g.label} · ${it.who === 'owner' ? 'propriétaire' : 'lead'}`, href: q({ vue, q: needle }), group: vue === 'decisions' ? 'Décisions du propriétaire' : 'Chargements en attente' });
          }
    }
    return hits.slice(0, 12);
  },
};
