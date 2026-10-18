/**
 * Décision — the decision studio (owner V8DECISION): "what happens if <actor> does <decision>?".
 * Route /v8/decision (?ref=<reference id> | composed fields; b.* = scenario B; tab/v/e/g/p = view), see state.ts.
 * API: /v1/v8/decision/* (src/reva/api/routes/v8_decision.py) over the V5 decision engine.
 */
import { lazy } from 'react';
import type { PaletteHit, V8Feature } from '../../contract';
import { v8Links } from '../../contract';

/** The five reference decisions (registry/predict/reference_decisions.yaml): ids are stable, labels short. */
export const REFERENCES: { id: string; label: string; keywords: string[] }[] = [
  { id: 'opec_plus_cut_2mbd_2027q1', label: 'OPEP+ réduit sa production de 2 mb/j (2027T1)', keywords: ['opep', 'opec', 'petrole', 'brut', 'oil', 'quota'] },
  { id: 'india_rice_export_ban_2027q1', label: 'L’Inde interdit les exportations de riz (2027T1, 1 an)', keywords: ['inde', 'india', 'riz', 'rice', 'export', 'interdiction'] },
  { id: 'ecb_plus_50bp_2027q1', label: 'La BCE relève ses taux de 50 pb (2027T1)', keywords: ['bce', 'ecb', 'taux', 'rate', 'politique monetaire'] },
  { id: 'eu_ban_russian_oil_2027q1', label: 'L’UE interdit le pétrole russe (2027T1)', keywords: ['ue', 'eu', 'russie', 'russia', 'embargo', 'sanctions', 'petrole'] },
  { id: 'france_retirement_65_2027', label: 'La France porte l’âge de la retraite à 65 ans (2027)', keywords: ['france', 'retraite', 'pension', 'age', '65'] },
];

const norm = (s: string) =>
  s
    .toLowerCase()
    .normalize('NFD')
    .replace(/[̀-ͯ]/g, '');

/** "bce 25" / "ecb +75bp" / "opep -1" → a variant of the matching reference with that magnitude. */
export function variantHref(query: string): { href: string; label: string } | null {
  const q = norm(query);
  const n = /([+-]?\d+(?:[.,]\d+)?)/.exec(q)?.[1]?.replace(',', '.');
  if (!n) return null;
  if (/\b(bce|ecb|taux)\b/.test(q)) {
    const m = n.replace(/^\+/, '');
    return { href: `/v8/decision?${new URLSearchParams({ ref: 'ecb_plus_50bp_2027q1', m }).toString()}`, label: `BCE ${Number(m) >= 0 ? '+' : ''}${m} pb (variante de la référence)` };
  }
  if (/\b(opep|opec)\b/.test(q)) {
    const m = n.startsWith('-') || n.startsWith('+') ? n.replace(/^\+/, '') : `-${n}`;
    return { href: `/v8/decision?${new URLSearchParams({ ref: 'opec_plus_cut_2mbd_2027q1', m }).toString()}`, label: `OPEP+ ${m} mb/j (variante de la référence)` };
  }
  return null;
}

export const feature: V8Feature = {
  id: 'decision',
  title: 'Décision',
  path: 'decision',
  order: 50,
  icon: '⎇',
  goKey: 's',
  element: lazy(() => import('./DecisionStudio')),
  commands: [
    ...REFERENCES.map((r) => ({ id: `decision.ref.${r.id}`, label: `Décision : ${r.label}`, keywords: ['decision', 'scenario', 'et si', ...r.keywords], run: () => v8Links.decision(r.id) })),
    {
      id: 'decision.compare.ecb',
      label: 'Comparer BCE +25 pb et +50 pb',
      keywords: ['comparer', 'bce', 'ecb', 'scenario'],
      run: () => `/v8/decision?${new URLSearchParams({ ref: 'ecb_plus_50bp_2027q1', m: '25', 'b.ref': 'ecb_plus_50bp_2027q1', tab: 'comparer' }).toString()}`,
    },
    {
      id: 'decision.variant',
      label: 'Décision : variante (« bce 25 », « opep -1 »)',
      keywords: ['decision', 'variante', 'bce', 'opep'],
      run: (q) => variantHref(q)?.href ?? v8Links.decision(),
    },
    { id: 'decision.new', label: 'Composer une décision', keywords: ['decision', 'composer', 'et si', 'what if'], run: () => v8Links.decision() },
  ],
  search: async (query: string): Promise<PaletteHit[]> => {
    const q = norm(query.trim());
    if (q.length < 2) return [];
    const hits: PaletteHit[] = REFERENCES.filter((r) => norm(r.label).includes(q) || r.keywords.some((k) => q.split(/\s+/).some((w) => w.length > 1 && k.startsWith(w)))).map((r) => ({
      id: `decision:${r.id}`,
      label: r.label,
      sublabel: 'Décision de référence · scénario non validé',
      href: v8Links.decision(r.id),
      group: 'Décisions',
    }));
    const v = variantHref(query);
    if (v) hits.unshift({ id: `decision:variant:${v.href}`, label: v.label, sublabel: 'Variante · scénario non validé', href: v.href, group: 'Décisions' });
    return hits.slice(0, 6);
  },
};
