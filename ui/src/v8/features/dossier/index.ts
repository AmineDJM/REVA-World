/**
 * Dossier (owner V8DOSSIER): one-click briefs — country, commodity, disease, drug, conflict, decision — built by the
 * server from declarative templates (registry/dossier/*.yaml): every sentence templated, every value footnoted with
 * its source, licence and period, every gap stated. Exports: Markdown, print-ready HTML (→ PDF), JSON.
 * URL: /v8/dossier (picker) and /v8/dossier/<kind>/<id> (one brief).
 */
import { lazy } from 'react';
import type { PaletteHit, V8Feature } from '../../contract';
import { dossierHref, getSubjects, KIND_LABEL } from './api';

const FEATURED: { kind: string; id: string; label: string; words: string[] }[] = [
  { kind: 'country', id: 'FRA', label: 'France', words: ['france', 'fra'] },
  { kind: 'commodity', id: 'crude_oil', label: 'Pétrole brut', words: ['pétrole', 'petrole', 'brut', 'brent', 'wti', 'oil', 'crude'] },
  { kind: 'disease', id: 'malaria', label: 'Paludisme', words: ['paludisme', 'malaria'] },
  { kind: 'drug', id: 'chembl:CHEMBL941', label: 'Imatinib', words: ['imatinib', 'glivec', 'gleevec'] },
  { kind: 'conflict', id: 'ucdp:conflict:13243', label: 'Russie – Ukraine', words: ['ukraine', 'russie', 'guerre'] },
  { kind: 'decision', id: 'opec_plus_cut_2mbd_2027q1', label: 'OPEP+ : −2 mb/j', words: ['opep', 'opec', 'quota', 'baisse'] },
];

const fold = (s: string) =>
  s
    .toLowerCase()
    .normalize('NFD')
    .replace(/[̀-ͯ]/g, '');

/** « dossier <sujet> » → the featured brief whose words match, else the picker with the query. */
export function dossierCommand(query: string): string {
  const q = fold(query.replace(/^\s*dossier\s*/i, '').trim());
  if (!q) return '/v8/dossier';
  if (/^[a-z]{3}$/.test(q)) return dossierHref('country', q.toUpperCase());
  const f = FEATURED.find((x) => x.words.some((w) => fold(w).startsWith(q) || q.includes(fold(w))));
  return f ? dossierHref(f.kind, f.id) : '/v8/dossier';
}

export const feature: V8Feature = {
  id: 'dossier',
  title: 'Dossier',
  path: 'dossier',
  navPath: 'dossier',
  order: 90,
  icon: '¶',
  goKey: 'b',
  element: lazy(() => import('./DossierPage')),
  commands: [
    { id: 'dossier.open', label: 'Dossier : ouvrir un dossier (ex. « dossier FRA », « dossier pétrole »)', keywords: ['dossier', 'brief', 'note', 'rapport', 'synthèse'], run: dossierCommand },
    ...FEATURED.map((f) => ({
      id: `dossier.${f.kind}.${f.id}`,
      label: `Dossier ${KIND_LABEL[f.kind]?.toLowerCase() ?? f.kind} : ${f.label}`,
      keywords: ['dossier', ...f.words],
      run: () => dossierHref(f.kind, f.id),
    })),
  ],
  search: async (query: string, signal: AbortSignal): Promise<PaletteHit[]> => {
    const q = query.trim();
    if (q.length < 3) return [];
    try {
      const res = await getSubjects(q, null, signal);
      return res.hits.slice(0, 6).map((h) => ({
        id: `dossier:${h.kind}:${h.id}`,
        label: `Dossier · ${h.label}`,
        sublabel: `${KIND_LABEL[h.kind] ?? h.kind}${h.sublabel ? ` · ${h.sublabel}` : ''}`,
        href: dossierHref(h.kind, h.id),
        group: 'Dossiers',
      }));
    } catch {
      return [];
    }
  },
};
