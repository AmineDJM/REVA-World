/**
 * Pure helpers of the Couverture page: what a matrix cell says, how strongly it is shaded, and the French wording.
 * A cell with nothing stored has NO intensity (it is hatched, never drawn as a low value): coverage ≠ truth, and the
 * absence of a source is not the absence of the phenomenon.
 */
import { frCompact, frNumber } from '../../shell/ui';
import type { Cell, Column, Summary } from './api';

export type Level = 'region' | 'subregion' | 'country';
export type Metric = 'pays' | 'lignes' | 'annee';

export const LEVELS: { id: Level; label: string }[] = [
  { id: 'region', label: 'Continents' },
  { id: 'subregion', label: 'Sous-régions' },
  { id: 'country', label: 'Pays' },
];

export const METRICS: { id: Metric; label: string; hint: string }[] = [
  { id: 'pays', label: 'Part des pays', hint: 'Part des pays de la colonne ayant au moins une ligne stockée dans ce domaine' },
  { id: 'lignes', label: 'Lignes', hint: 'Nombre de lignes stockées (échelle logarithmique)' },
  { id: 'annee', label: 'Dernière année', hint: 'Année la plus récente observée (périodes passées seulement ; les projections sont exclues)' },
];

export const NOTE_SHORT = 'Couverture ≠ vérité : l’absence d’une source n’est pas l’absence du phénomène.';

export function isLevel(v: string): v is Level {
  return v === 'region' || v === 'subregion' || v === 'country';
}
export function isMetric(v: string): v is Metric {
  return v === 'pays' || v === 'lignes' || v === 'annee';
}

/** True when the cell stores at least one atlas row (KG-only cells are drawn differently). */
export function hasRows(c: Cell | undefined): c is Cell {
  return !!c && (c.rows ?? 0) > 0;
}

/** Shading in [0.08, 1], or null when nothing is stored (hatched). */
export function intensity(c: Cell | undefined, col: Column, metric: Metric, maxRows: number, thisYear: number): number | null {
  if (!c) return null;
  if (!hasRows(c)) return c.kg ? 0.08 : null;
  if (metric === 'pays') {
    const n = c.countries_with_rows ?? 1;
    return clamp(n / Math.max(1, col.countries));
  }
  if (metric === 'lignes') {
    const r = c.rows ?? 0;
    return clamp(Math.log10(1 + r) / Math.log10(1 + Math.max(10, maxRows)));
  }
  const y = c.y1 ?? null;
  if (y === null) return 0.08; // only future-dated rows (projections): stored, but nothing observed
  const age = thisYear - y;
  return clamp(1 - Math.min(age, 25) / 25);
}

function clamp(x: number): number {
  return Math.max(0.08, Math.min(1, x));
}

/** The short text drawn in a cell. */
export function cellText(c: Cell | undefined, col: Column, metric: Metric, level: Level): string {
  if (!c) return '';
  if (!hasRows(c)) return c.kg ? 'KG' : '';
  if (level === 'country') {
    if (metric === 'annee') return c.y1 != null ? String(c.y1).slice(-2) : '·';
    return '';
  }
  if (metric === 'pays') return `${c.countries_with_rows ?? 0}/${col.countries}`;
  if (metric === 'lignes') return frCompact(c.rows ?? 0);
  return c.y1 != null ? String(c.y1) : '—';
}

/** Full French description of a cell (tooltip + accessible name). */
export function cellTitle(rowLabel: string, col: Column, c: Cell | undefined, level: Level): string {
  const where = level === 'country' ? col.label : `${col.label} (${col.countries} pays)`;
  if (!c) return `${rowLabel} · ${where} : aucune source chargée — ce n’est pas l’absence du phénomène.`;
  const parts: string[] = [];
  if (hasRows(c)) {
    if (level !== 'country') parts.push(`${c.countries_with_rows ?? 0} pays sur ${col.countries} avec des données`);
    parts.push(`${frNumber(c.rows ?? 0)} lignes`, `${c.tables ?? 0} table${(c.tables ?? 0) > 1 ? 's' : ''}`);
    if (c.y0 != null && c.y1 != null) parts.push(`${c.y0}–${c.y1}`);
    else if (c.y1 != null) parts.push(`dernière année ${c.y1}`);
    if (c.y1 == null) parts.push('seulement des périodes futures (projections)');
    if ((c.future_rows ?? 0) > 0) parts.push(`${frNumber(c.future_rows ?? 0)} lignes datées après aujourd’hui (projections)`);
  } else {
    parts.push('aucune ligne d’atlas');
  }
  if (c.kg) parts.push(`${frNumber(c.kg)} entités KG rattachées`);
  return `${rowLabel} · ${where} : ${parts.join(' · ')}`;
}

export const LICENCE_LABEL: Record<string, string> = {
  open: 'ouverte',
  non_commercial: 'non commerciale',
  restricted: 'restreinte',
  unknown: 'non déterminée',
};

export const LICENCE_ORDER = ['open', 'unknown', 'restricted', 'non_commercial'];

export function licenceBreakdown(s: Summary): { cls: string; n: number; label: string }[] {
  return LICENCE_ORDER.filter((k) => (s.licences[k] ?? 0) > 0).map((k) => ({ cls: k, n: s.licences[k] ?? 0, label: LICENCE_LABEL[k] ?? k }));
}

export function frDate(iso: string | null | undefined): string {
  if (!iso) return '';
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  return d.toLocaleDateString('fr-FR', { day: 'numeric', month: 'short', year: 'numeric' });
}

export function frDateTime(iso: string | null | undefined): string {
  if (!iso) return '';
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  return d.toLocaleString('fr-FR', { day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit', timeZone: 'UTC' }) + ' UTC';
}

export function frCadence(s: number | null | undefined): string {
  if (!s) return 'cadence non déclarée';
  const d = s / 86400;
  if (d <= 1.5) return 'quotidienne';
  if (d <= 8) return 'hebdomadaire';
  if (d <= 40) return 'mensuelle';
  if (d <= 100) return 'trimestrielle';
  if (d <= 200) return 'semestrielle';
  return 'annuelle ou moins';
}

export function frMb(mb: number | null | undefined): string {
  if (mb == null) return 'taille non estimée';
  if (mb >= 1000) return `${frNumber(mb / 1000, 1)} Go`;
  return `${frNumber(mb, 0)} Mo`;
}

export const STATE_LABEL: Record<string, string> = {
  done: 'exécution constatée',
  partial: 'partiellement constaté',
  not_seen: 'pas encore constaté',
};

export const STATUS_LABEL: Record<string, string> = {
  open: 'ouverte',
  queued: 'en file',
  blocked: 'bloqué',
  done: 'fait',
  dropped: 'abandonné',
};

/** Rows-per-year histogram as [year, rows] sorted, from `fromYear` (gaps are real gaps, not zeros drawn). */
export function histPoints(hist: Record<string, number>, fromYear = 1950, toYear?: number): [number, number][] {
  return Object.entries(hist)
    .map(([y, n]) => [Number(y), n] as [number, number])
    .filter(([y]) => Number.isFinite(y) && y >= fromYear && (toYear === undefined || y <= toYear))
    .sort((a, b) => a[0] - b[0]);
}

/** Filter pending items by free text (title, action, what it unlocks, commands). */
export function matchItem(q: string, hay: string[]): boolean {
  const n = q.trim().toLowerCase();
  if (!n) return true;
  const h = hay.join(' ').toLowerCase();
  return n.split(/\s+/).every((w) => h.includes(w));
}

/** Domain labels (filled by the page from the matrix rows) for chips and lists outside the matrix. */
const DOMAIN_FR = new Map<string, string>();
export function setDomainLabels(rows: { id: string; label: string }[]): void {
  for (const r of rows) DOMAIN_FR.set(r.id, r.label);
}
export function domainLabel(id: string): string {
  return DOMAIN_FR.get(id) ?? (id === 'other' ? 'Non classé' : id);
}
