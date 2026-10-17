/** Pure helpers of the "Décision" screen (no React): composing a decision, French labels, number formatting. */
import type { DecisionDoc, Hop, PeriodValue, Prediction, VariableResult } from './api';

export interface DecisionForm {
  id: string;
  label_fr: string;
  actor_label: string;
  actor_geo: string;
  target_label: string;
  target_geo: string;
  instrument: string;
  magnitude: string;
  unit: string;
  start: string;
  duration: string; // '' = held over the horizon
  horizon: string;
  countries: string;
}

export function formFromDecision(d: DecisionDoc): DecisionForm {
  return {
    id: d.id,
    label_fr: d.label_fr,
    actor_label: d.actor.label_fr,
    actor_geo: d.actor.geo ?? '',
    target_label: d.target.label_fr,
    target_geo: d.target.geo ?? '',
    instrument: d.instrument,
    magnitude: d.magnitude.value,
    unit: d.magnitude.unit,
    start: d.start,
    duration: d.duration_quarters == null ? '' : String(d.duration_quarters),
    horizon: String(d.horizon_quarters),
    countries: d.countries.join(', '),
  };
}

export const QUARTER_RE = /^\d{4}Q[1-4]$/;
const ISO3 = /^[A-Z]{3}$/;

/** Build the request decision; returns French problems instead when the form is invalid. */
export function composeDecision(f: DecisionForm): { decision?: Record<string, unknown>; problems: string[] } {
  const problems: string[] = [];
  const countries = f.countries
    .split(/[\s,;]+/)
    .map((c) => c.trim().toUpperCase())
    .filter(Boolean);
  if (!/^[+-]?(\d+\.?\d*|\.\d+)$/.test(f.magnitude.trim())) problems.push('La grandeur doit être un nombre décimal (ex. -2 ou 50).');
  if (!QUARTER_RE.test(f.start.trim())) problems.push('Le début doit être un trimestre de la forme 2027Q1.');
  for (const c of countries) if (!ISO3.test(c)) problems.push(`« ${c} » n’est pas un code pays ISO 3166 à trois lettres.`);
  const horizon = Number(f.horizon);
  if (!Number.isInteger(horizon) || horizon < 1 || horizon > 12) problems.push('L’horizon va de 1 à 12 trimestres.');
  const duration = f.duration.trim() === '' ? null : Number(f.duration);
  if (duration !== null && (!Number.isInteger(duration) || duration < 1)) problems.push('La durée est un nombre entier de trimestres (vide = toute la période).');
  for (const [geo, what] of [
    [f.actor_geo, 'acteur'],
    [f.target_geo, 'cible'],
  ] as const) {
    if (geo.trim() && !ISO3.test(geo.trim().toUpperCase())) problems.push(`Le pays ${what} doit être un code ISO3 (ou vide).`);
  }
  if (!f.label_fr.trim()) problems.push('Donnez un intitulé à la décision.');
  if (problems.length) return { problems };
  const slug =
    f.id.trim() ||
    f.label_fr
      .toLowerCase()
      .normalize('NFD')
      .replace(/[^a-z0-9]+/g, '_')
      .replace(/^_+|_+$/g, '')
      .slice(0, 60) ||
    'decision';
  return {
    problems,
    decision: {
      id: /^[a-z0-9][a-z0-9_.-]{0,99}$/.test(slug) ? slug : 'decision',
      label_fr: f.label_fr.trim(),
      actor: { label_fr: f.actor_label.trim() || 'Acteur', geo: f.actor_geo.trim().toUpperCase() || null },
      instrument: f.instrument,
      target: { label_fr: f.target_label.trim() || 'Cible', geo: f.target_geo.trim().toUpperCase() || null },
      magnitude: { value: f.magnitude.trim(), unit: f.unit },
      start: f.start.trim(),
      duration_quarters: duration,
      horizon_quarters: horizon,
      countries,
    },
  };
}

const NF = new Intl.NumberFormat('fr-FR', { maximumSignificantDigits: 3 });
export function fmt(x: number | null | undefined): string {
  if (x === null || x === undefined || !Number.isFinite(x)) return '—';
  return NF.format(x);
}

const UNIT_FR: Record<string, string> = {
  percent: '%',
  pp: 'pt de %',
  log_points: 'points log',
  pp_gdp: '% du PIB',
  occurrence: 'survenue',
};
export function unitFr(u: string): string {
  if (u in UNIT_FR) return UNIT_FR[u]!;
  if (u.startsWith('level:')) return u.slice(6);
  return u;
}

export function variableLabel(v: VariableResult): string {
  return v.variable.label_fr || v.variable.label_en || v.variable.id;
}

export const GEO_FR: Record<string, string> = { WLD: 'Monde' };
export function geoFr(g: string): string {
  return GEO_FR[g] ?? g;
}

const GAP_FR: Record<string, string> = {
  no_effect_estimate_for_cause: 'Aucune estimation d’effet n’a cette variable pour cause : la propagation s’arrête.',
  plugin_data_missing: 'Le modèle ne peut pas tourner : ses données d’entrée ne sont pas dans la publication de référence.',
  missing_input: 'Entrée manquante (jamais remplacée par zéro).',
  variable_not_in_effect_vocabulary: 'Variable absente du vocabulaire des effets : aucune estimation ne peut la citer.',
  baseline_unavailable: 'Pas de trajectoire de référence (atlas non disponible).',
  max_depth: 'Profondeur maximale de propagation atteinte.',
  upstream_missing: 'Valeur amont manquante.',
  plugin_input_missing: 'Entrée du modèle manquante sur la première année.',
  io_country_not_in_table: 'Pays absent du tableau entrées-sorties.',
};
export function gapFr(g: { code: string; message?: unknown; message_fr?: unknown }): string {
  const own = typeof g.message_fr === 'string' ? g.message_fr : null;
  return own ?? GAP_FR[g.code] ?? (typeof g.message === 'string' ? g.message : g.code);
}

const MISSING_FR: [RegExp, string][] = [
  [/^lag_not_published/, 'délai non publié par la source (jamais interpolé)'],
  [/^upstream_missing/, 'valeur amont manquante'],
];
export function missingFr(reason: string): string {
  for (const [re, t] of MISSING_FR) if (re.test(reason)) return t;
  return reason;
}

export const APPLICABILITY_FR: Record<string, string> = {
  exact: 'champ de l’estimation : ce pays',
  world_panel: 'panel mondial transposé au pays',
  group_unverified: 'groupe de pays (appartenance non vérifiée)',
  scope_unstated: 'champ non précisé par la source',
};

/** Hops from the decision to a variable, in order. */
export function pathOf(v: VariableResult, hops: Hop[]): Hop[] {
  const byId = new Map(hops.map((h) => [h.id, h] as const));
  return v.path.map((id) => byId.get(id)).filter((h): h is Hop => h !== undefined);
}

/** Periods with a value (fan chart input); missing ones are listed apart, never drawn as zero. */
export function bands(periods: PeriodValue[]): { drawn: (PeriodValue & { p50: number; p05: number; p95: number; p25: number; p75: number })[]; missing: PeriodValue[] } {
  const drawn: (PeriodValue & { p50: number; p05: number; p95: number; p25: number; p75: number })[] = [];
  const missing: PeriodValue[] = [];
  for (const p of periods) {
    if (p.missing_reason || p.p50 == null || p.p05 == null || p.p95 == null || p.p25 == null || p.p75 == null) missing.push(p);
    else drawn.push(p as PeriodValue & { p50: number; p05: number; p95: number; p25: number; p75: number });
  }
  return { drawn, missing };
}

export function isValidated(p: Prediction): boolean {
  return p.validation.status === 'validated';
}
