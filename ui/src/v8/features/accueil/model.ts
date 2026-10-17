/** Pure helpers of the Accueil page (labels, windows, grouping) — tested in accueil.test.tsx. */
import type { ChangeItem } from './api';

export const KIND_FR: Record<string, { label: string; short: string; glyph: string }> = {
  ingest: { label: 'Données chargées', short: 'Données', glyph: '⇣' },
  atlas_snapshot: { label: 'Instantané de l’atlas', short: 'Atlas', glyph: '◫' },
  kg_batch: { label: 'Graphe de connaissances', short: 'Graphe', glyph: '◇' },
  causal_claims: { label: 'Affirmations causales', short: 'Causal', glyph: '→' },
  effects: { label: 'Estimations d’effet', short: 'Effets', glyph: 'Δ' },
  forecast_batch: { label: 'Prévisions scellées', short: 'Scellées', glyph: '⊡' },
  question_resolved: { label: 'Questions résolues', short: 'Résolues', glyph: '✓' },
  evaluation: { label: 'Évaluations', short: 'Évaluations', glyph: '≈' },
  owner_decision: { label: 'Décisions du propriétaire', short: 'Décisions', glyph: '!' },
};

export const KIND_ORDER = ['ingest', 'question_resolved', 'forecast_batch', 'causal_claims', 'effects', 'kg_batch', 'atlas_snapshot', 'evaluation', 'owner_decision'];

export function kindFr(kind: string) {
  return KIND_FR[kind] ?? { label: kind, short: kind, glyph: '•' };
}

const COUNT_FR: Record<string, [string, string]> = {
  rows: ['ligne', 'lignes'],
  tables: ['table', 'tables'],
  entities: ['entité', 'entités'],
  statements: ['déclaration', 'déclarations'],
  added: ['ajoutée', 'ajoutées'],
  retracted: ['retirée', 'retirées'],
  claims: ['affirmation', 'affirmations'],
  estimates: ['estimation', 'estimations'],
  retired: ['retirée', 'retirées'],
  questions: ['question', 'questions'],
  forecasts: ['prévision', 'prévisions'],
  resolved: ['résolue', 'résolues'],
  void: ['annulée', 'annulées'],
  revisions: ['révision', 'révisions'],
  loads: ['lot', 'lots'],
  retractions: ['retrait', 'retraits'],
  graded: ['notée', 'notées'],
  models: ['modèle', 'modèles'],
  forecasters_scored: ['prévisionniste noté', 'prévisionnistes notés'],
};

/** "30 187 lignes · 1 table" — integers only, French grouping, zeros omitted; unknown keys keep their name. */
export function countsFr(counts: Record<string, number>): string {
  return Object.entries(counts)
    .filter(([, n]) => Number.isFinite(n) && n !== 0)
    .map(([k, n]) => {
      const w = COUNT_FR[k];
      return `${n.toLocaleString('fr-FR')} ${w ? (Math.abs(n) > 1 ? w[1] : w[0]) : k}`;
    })
    .join(' · ');
}

export const WINDOWS = { '24h': 24, '7j': 24 * 7, '30j': 24 * 30 } as const;
export type WindowKey = keyof typeof WINDOWS;
export const WINDOW_FR: Record<WindowKey, string> = { '24h': '24 h', '7j': '7 jours', '30j': '30 jours' };

export function sinceFor(w: WindowKey, now = Date.now()): string {
  // rounded to the minute so the URL-derived request is stable within a minute (cache, StrictMode)
  const t = now - WINDOWS[w] * 3600_000;
  return new Date(Math.floor(t / 60_000) * 60_000).toISOString();
}

export function asWindow(s: string | null | undefined): WindowKey {
  return s === '7j' || s === '30j' ? s : '24h';
}

/** Day bucket label: Aujourd'hui / Hier / "lun. 12 oct.". */
export function dayLabel(iso: string, now = new Date()): string {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso.slice(0, 10);
  const day = (x: Date) => new Date(x.getFullYear(), x.getMonth(), x.getDate()).getTime();
  const diff = Math.round((day(now) - day(d)) / 86400_000);
  if (diff === 0) return 'Aujourd’hui';
  if (diff === 1) return 'Hier';
  return d.toLocaleDateString('fr-FR', { weekday: 'short', day: 'numeric', month: 'short' });
}

export function groupByDay(items: readonly ChangeItem[], now = new Date()): { day: string; items: ChangeItem[] }[] {
  const out: { day: string; items: ChangeItem[] }[] = [];
  for (const it of items) {
    const label = dayLabel(it.at, now);
    const last = out[out.length - 1];
    if (last && last.day === label) last.items.push(it);
    else out.push({ day: label, items: [it] });
  }
  return out;
}

export function timeFr(iso: string): string {
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? '' : d.toLocaleTimeString('fr-FR', { hour: '2-digit', minute: '2-digit' });
}

export function dateFr(iso: string | null | undefined, opts: Intl.DateTimeFormatOptions = { day: 'numeric', month: 'short', year: 'numeric' }): string {
  if (!iso) return '';
  if (/^\d{4}-\d{2}$/.test(iso)) {
    const [y, m] = iso.split('-');
    return new Date(Number(y), Number(m) - 1, 1).toLocaleDateString('fr-FR', { month: 'long', year: 'numeric' });
  }
  const d = new Date(iso.length === 10 ? `${iso}T00:00:00` : iso);
  return Number.isNaN(d.getTime()) ? iso : d.toLocaleDateString('fr-FR', opts);
}

/** Days from today to an ISO date (negative = past). */
export function daysUntil(iso: string, now = new Date()): number {
  const d = new Date(iso.length === 10 ? `${iso}T00:00:00` : iso);
  const t0 = new Date(now.getFullYear(), now.getMonth(), now.getDate()).getTime();
  return Math.round((d.getTime() - t0) / 86400_000);
}

export function relDays(n: number): string {
  if (n === 0) return 'aujourd’hui';
  if (n === 1) return 'demain';
  if (n === -1) return 'hier';
  return n > 0 ? `dans ${n} j` : `il y a ${-n} j`;
}

/**
 * The judged status of a resolved question as the change feed states it (never computed here): void questions say
 * why; a resolution without a sealed official forecaster says there is no comparison; with one, the comparison is
 * on the scoreboard (one question is not a verdict).
 */
export function verdictOf(it: ChangeItem): { text: string; kind: 'win' | 'loss' | 'neutral' | 'missing' } | null {
  if (it.kind !== 'question_resolved') return null;
  const d = it.detail;
  const v = typeof d.verdict_fr === 'string' ? d.verdict_fr : typeof d.verdict === 'string' ? d.verdict : null;
  if (v) {
    const lv = v.toLowerCase();
    const kind = /ne bat pas|perd|loses|worse/.test(lv) ? 'loss' : /bat|beats|better|gagne/.test(lv) ? 'win' : 'neutral';
    return { text: v, kind };
  }
  if (d.status === 'void') return { text: `annulée${it.summary ? ` : ${it.summary.replace(/_/g, ' ')}` : ''}`, kind: 'missing' };
  if (d.official === 'absent') return { text: 'aucun prévisionniste officiel scellé : pas de comparaison', kind: 'missing' };
  if (d.official && typeof d.official === 'object') {
    const names = Object.keys(d.official as Record<string, unknown>);
    if (names.length) return { text: `comparée à ${names.join(', ')} — une question n’est pas un verdict`, kind: 'neutral' };
  }
  return null;
}

/** Kinds shown by default in the feed: owner decisions have their own panel. */
export const DEFAULT_FEED_KINDS = KIND_ORDER.filter((k) => k !== 'owner_decision');

/** Day-key of a date in YYYY-MM-DD (local), for the calendar grouping. */
export function isoDay(d: Date): string {
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`;
}
