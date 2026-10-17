/**
 * Pure helpers of the Scores feature: wording, number formatting (French), calendar dates, palette commands.
 * No number is invented here: a missing ratio stays missing (the caller renders it as such).
 */
import type { PaletteCommand } from '../../contract';
import type { CalendarItem, Counts, Row, Verdict } from './api';

export const VERDICT_FR: Record<Verdict, string> = { bat: 'bat', ne_bat_pas: 'ne bat pas', egalite: 'égalité', en_attente: 'en attente' };
export const VERDICT_ORDER: Verdict[] = ['bat', 'ne_bat_pas', 'egalite', 'en_attente'];
export const VERDICT_TONE: Record<Verdict, 'win' | 'loss' | 'neutral' | 'pending'> = { bat: 'win', ne_bat_pas: 'loss', egalite: 'neutral', en_attente: 'pending' };

export const EVALUATION_SHORT: Record<string, string> = {
  development: 'dév.',
  preregistered_holdout: 'holdout pré-enr.',
  preregistered_backtest: 'pré-enr. (vintages)',
  prospective: 'prospectif',
};

export const METRIC_SHORT: Record<string, string> = { ae: 'AE', crps: 'CRPS', wis: 'WIS', brier: 'Brier', logloss: 'log loss', msle: 'MSLE', crps_log1p: 'CRPS log', mixed: 'mixte' };

/** Forecaster ids → words people type in the palette. */
export const FORECASTER_KEYWORDS: Record<string, string[]> = {
  imf_weo: ['fmi', 'imf', 'weo', 'fonds monétaire'],
  us_spf: ['spf', 'philadelphie', 'philadelphia', 'fed'],
  ecb_spf: ['bce', 'ecb', 'spf bce'],
  eia_steo: ['eia', 'steo', 'pétrole', 'brent', 'wti'],
  cdc_flusight: ['cdc', 'flusight', 'grippe'],
  cdc_covidhub: ['cdc', 'covid', 'covidhub'],
  views: ['views', 'conflit', 'uppsala'],
  atlanta_gdpnow: ['gdpnow', 'atlanta'],
  nyfed_nowcast: ['ny fed', 'new york'],
  cleveland_nowcast: ['cleveland'],
  usda_wasde: ['wasde', 'usda', 'agriculture'],
  ecdc_respicast: ['ecdc', 'respicast'],
};

export const DOMAIN_KEYWORDS: Record<string, string[]> = {
  macro: ['macro', 'macroéconomie', 'pib', 'inflation', 'chômage'],
  nowcast: ['nowcast', 'nowcasting'],
  energy: ['énergie', 'energie', 'pétrole', 'gaz'],
  health: ['santé', 'sante', 'grippe', 'covid', 'hospitalisations'],
  conflict: ['conflit', 'conflits', 'guerre'],
  agri: ['agriculture', 'agri', 'récoltes'],
  finance: ['finance', 'taux', 'change'],
};

const FORECASTER_LABEL: Record<string, string> = {
  imf_weo: 'FMI (WEO)', us_spf: 'SPF (Fed de Philadelphie)', ecb_spf: 'SPF BCE', eia_steo: 'EIA (STEO)', cdc_flusight: 'CDC FluSight',
  cdc_covidhub: 'CDC COVID-19 Hub', views: 'ViEWS', atlanta_gdpnow: 'GDPNow', nyfed_nowcast: 'NY Fed Nowcast', cleveland_nowcast: 'Cleveland Fed',
  usda_wasde: 'USDA WASDE', ecdc_respicast: 'ECDC RespiCast',
};
const DOMAIN_LABEL: Record<string, string> = { macro: 'macroéconomie', nowcast: 'nowcasting', energy: 'énergie', health: 'santé', conflict: 'conflits', agri: 'agriculture', finance: 'finance' };

/** "×0,63" (lower is better for REVA); null → null (caller shows it as absent). */
export function fmtRatio(r: number | null | undefined): string | null {
  if (r === null || r === undefined || !Number.isFinite(r)) return null;
  return `×${r.toLocaleString('fr-FR', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
}

export function fmtRange(lo: number | null | undefined, hi: number | null | undefined): string | null {
  const a = fmtRatio(lo);
  const b = fmtRatio(hi);
  if (!a) return null;
  if (!b || a === b) return a;
  return `${a}–${b.slice(1)}`;
}

/** p-values: 3 significant digits, scientific below 0.001 ("2,1e-7"). */
export function fmtP(p: number | null | undefined): string | null {
  if (p === null || p === undefined || !Number.isFinite(p)) return null;
  if (p >= 0.001) return p.toLocaleString('fr-FR', { maximumSignificantDigits: 2 });
  const [m = '', e = ''] = p.toExponential(1).split('e');
  return `${m.replace('.', ',')}e${e}`;
}

export function fmtPct(x: number | null | undefined): string | null {
  if (x === null || x === undefined || !Number.isFinite(x)) return null;
  return `${(x * 100).toLocaleString('fr-FR', { maximumFractionDigits: 0 })} %`;
}

export function total(c: Counts | null | undefined): number {
  return c ? c.bat + c.ne_bat_pas + c.egalite + c.en_attente : 0;
}

const MONTHS = ['janv.', 'févr.', 'mars', 'avr.', 'mai', 'juin', 'juil.', 'août', 'sept.', 'oct.', 'nov.', 'déc.'];

/** "22 oct. 2026", "déc. 2026", "après le 30 avr. 2027", "date non fixée". */
export function whenLabel(item: Pick<CalendarItem, 'date' | 'month' | 'not_before'>): string {
  if (item.date) return dayLabel(item.date);
  if (item.not_before) return `après le ${dayLabel(item.not_before)}`;
  if (item.month) {
    const [y, m] = item.month.split('-').map(Number);
    return `${MONTHS[(m || 1) - 1]} ${y}`;
  }
  return 'date non fixée';
}

export function dayLabel(iso: string): string {
  const [y, m, d] = iso.slice(0, 10).split('-').map(Number);
  if (!y || !m || !d) return iso;
  return `${d} ${MONTHS[m - 1]} ${y}`;
}

/** Days from `today` to an ISO date (negative = past); null when undated. */
export function daysUntil(iso: string | null | undefined, today: Date): number | null {
  if (!iso) return null;
  const t = Date.UTC(Number(iso.slice(0, 4)), Number(iso.slice(5, 7)) - 1, Number(iso.slice(8, 10)) || 1);
  const n = Date.UTC(today.getUTCFullYear(), today.getUTCMonth(), today.getUTCDate());
  return Math.round((t - n) / 86_400_000);
}

/** One-line honest wording of a row: "<candidat> <verdict> <repère> (<métrique>, ×r)". */
export function rowSentence(r: Row): string {
  const ratio = fmtRatio(r.ratio) ?? fmtRange(r.ratio_range?.[0], r.ratio_range?.[1]);
  const v = r.verdict === 'en_attente' ? 'sera jugé contre' : r.verdict === 'egalite' ? 'à égalité avec' : `${VERDICT_FR[r.verdict]}`;
  return `${r.candidate_label_fr ?? r.candidate ?? '?'} ${v} ${r.benchmark_label_fr ?? r.benchmark ?? ''}${ratio ? ` (${METRIC_SHORT[r.metric] ?? r.metric}, ${ratio})` : ''}`;
}

export function forecasterLabel(id: string | null | undefined): string {
  if (!id) return 'référence naïve';
  return FORECASTER_LABEL[id] ?? id;
}

/** Palette commands: « scores FMI », « scores énergie », « calendrier des résolutions », « comment REVA est jugée ». */
export function buildCommands(): PaletteCommand[] {
  const out: PaletteCommand[] = [
    { id: 'scores.home', label: 'Scores : REVA contre les prévisionnistes officiels', keywords: ['scores', 'score', 'bat', 'tableau des scores', 'prévisions jugées'], run: () => '/v8/scores' },
    { id: 'scores.calendar', label: 'Scores : calendrier des résolutions', keywords: ['calendrier', 'résolutions', 'en attente', 'scores calendrier'], run: () => '/v8/scores?section=calendrier' },
    { id: 'scores.explainer', label: 'Scores : comment REVA est jugée', keywords: ['jugée', 'holm', 'pré-enregistrement', 'méthode', 'scores méthode'], run: () => '/v8/scores?section=methode' },
  ];
  for (const [id, kws] of Object.entries(FORECASTER_KEYWORDS)) {
    out.push({ id: `scores.f.${id}`, label: `Scores : REVA contre ${FORECASTER_LABEL[id] ?? id}`, keywords: [...kws.map((k) => `scores ${k}`), ...kws], run: () => `/v8/scores?f=${id}` });
  }
  for (const [id, kws] of Object.entries(DOMAIN_KEYWORDS)) {
    out.push({ id: `scores.d.${id}`, label: `Scores : ${DOMAIN_LABEL[id] ?? id}`, keywords: kws.map((k) => `scores ${k}`), run: () => `/v8/scores?d=${id}` });
  }
  return out;
}

/** Short French reason a row does not count in the headline (or null when it does). */
export function notCountedReason(r: Pick<Row, 'benchmark_kind' | 'quarantined'>): string | null {
  if (r.quarantined) return 'ligne en quarantaine : sa source ne la confirme plus';
  if (r.benchmark_kind === 'weak_reference') return 'repère faible : non compté';
  if (r.benchmark_kind === 'naive') return 'référence naïve : comptée à part';
  return null;
}
