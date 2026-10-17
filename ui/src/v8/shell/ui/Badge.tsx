import type { ReactNode } from 'react';

/**
 * Honesty kinds (constitution): every value that is not a plain observation says what it is.
 *  observed   → "observé"                 (reported by a source)
 *  projection → "projection"              (a model/official projection; dashed in charts)
 *  estimate   → "estimation"              (an estimate, e.g. an effect size, a nowcast)
 *  scenario   → "scénario non validé"     (what-if propagation; never a probability)
 *  judged     → "prévision jugée"         (a sealed forecast with its score vs the official forecaster)
 *  missing    → "manquant"                (typed Missing; use <MissingValue/> for cells)
 * Extra kinds: community, fixture, licence (non commercial…), win, loss, neutral, info.
 */
export type HonestyKind = 'observed' | 'projection' | 'estimate' | 'scenario' | 'judged' | 'missing';
export type BadgeKind = HonestyKind | 'community' | 'fixture' | 'licence' | 'win' | 'loss' | 'neutral' | 'info';

export const HONESTY_LABEL: Record<HonestyKind, string> = {
  observed: 'observé',
  projection: 'projection',
  estimate: 'estimation',
  scenario: 'scénario non validé',
  judged: 'prévision jugée',
  missing: 'manquant',
};

const DEFAULT_TITLE: Partial<Record<BadgeKind, string>> = {
  observed: 'Valeur rapportée par la source citée.',
  projection: 'Projection (d’un modèle ou d’un organisme) : pas une observation.',
  estimate: 'Estimation : pas une observation directe ; voir la méthode et l’incertitude.',
  scenario: 'Scénario non validé : propagation « et si… », pas une probabilité ni une prévision.',
  judged: 'Prévision scellée avant la résolution et jugée contre la prévision officielle.',
  missing: 'Donnée absente : la raison est indiquée ; jamais remplacée par 0.',
  community: 'Base collaborative (p. ex. Wikidata) : REVA ne vérifie pas ces déclarations.',
  fixture: 'Donnée de test (origin = test_fixture), pas une donnée publiée.',
  licence: 'Conditions de licence restrictives : voir la source.',
};

const DEFAULT_TEXT: Partial<Record<BadgeKind, string>> = {
  ...HONESTY_LABEL,
  community: 'communautaire, non vérifié',
  fixture: 'donnée de test',
  licence: 'licence non commerciale',
};

export function Badge({ kind, children, title, size = 'sm' }: { kind: BadgeKind; children?: ReactNode; title?: string; size?: 'xs' | 'sm' }) {
  const text = children ?? DEFAULT_TEXT[kind] ?? kind;
  return (
    <span className={`v8-badge v8-badge-${kind} v8-badge-${size}`} title={title ?? DEFAULT_TITLE[kind]} data-honesty={kind}>
      <span className="v8-badge-dot" aria-hidden="true" />
      {text}
    </span>
  );
}
