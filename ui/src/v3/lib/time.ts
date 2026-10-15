/**
 * Time helpers for KG statements.
 *
 * KG times use astronomical year numbering (year 0 = 1 BCE, -0043 = 44 BCE) with a Wikibase
 * precision (9 year, 10 month, 11 day; 8 decade, 7 century, 6 millennium...). Display uses the
 * historical convention ("44 av. J.-C."). Nothing here invents a date: an undated statement stays
 * undated (it is not "always").
 */
import type { KgStatement, KgTime } from '../api/kg';

/** Astronomical year of a time string ("1494-06-07T00:00:00Z", "-0043-03-15", "0044-03-15 BC"). */
export function yearOf(time: string | null | undefined): number | null {
  if (!time) return null;
  const t = time.trim();
  const m = /^([+-]?)(\d{1,7})/.exec(t);
  if (!m) return null;
  let y = Number(m[2]);
  if (m[1] === '-') y = -y;
  if (/\bBC$/i.test(t)) y = 1 - y; // PostgreSQL "0044-03-15 BC" (historical) -> astronomical -43
  return Number.isFinite(y) ? y : null;
}

/** "1494", "44 av. J.-C." (astronomical year -> historical label). */
export function formatYear(y: number | null | undefined): string {
  if (y === null || y === undefined || !Number.isFinite(y)) return '?';
  return y <= 0 ? `${1 - y} av. J.-C.` : String(y);
}

const MONTHS = ['janv.', 'févr.', 'mars', 'avr.', 'mai', 'juin', 'juil.', 'août', 'sept.', 'oct.', 'nov.', 'déc.'];

/** A KG time at its own precision: "7 juin 1494", "juin 1494", "1494", "années 1490", "XVe siècle". */
export function formatKgTime(t: KgTime | null | undefined): string {
  if (!t) return '';
  const y = yearOf(t.time);
  if (y === null) return t.time;
  const m = /^[+-]?\d{1,7}-(\d{2})-(\d{2})/.exec(t.time.trim());
  const month = m ? Number(m[1]) : 0;
  const day = m ? Number(m[2]) : 0;
  const p = t.precision;
  if (p >= 11 && month && day) return `${day} ${MONTHS[month - 1] ?? ''} ${formatYear(y)}`;
  if (p === 10 && month) return `${MONTHS[month - 1] ?? ''} ${formatYear(y)}`;
  if (p === 9) return formatYear(y);
  if (p === 8) return `années ${formatYear(Math.floor(y / 10) * 10)}`;
  if (p === 7) {
    const c = y > 0 ? Math.floor((y - 1) / 100) + 1 : Math.floor(-y / 100) + 1;
    return `${roman(c)}e siècle${y <= 0 ? ' av. J.-C.' : ''}`;
  }
  if (p === 6) {
    const k = y > 0 ? Math.floor((y - 1) / 1000) + 1 : Math.floor(-y / 1000) + 1;
    return `${k}${k === 1 ? 'er' : 'e'} millénaire${y <= 0 ? ' av. J.-C.' : ''}`;
  }
  return `vers ${formatYear(y)} (précision ${p})`;
}

export function roman(n: number): string {
  const table: [number, string][] = [
    [1000, 'M'], [900, 'CM'], [500, 'D'], [400, 'CD'], [100, 'C'], [90, 'XC'], [50, 'L'], [40, 'XL'], [10, 'X'], [9, 'IX'], [5, 'V'], [4, 'IV'], [1, 'I'],
  ];
  let out = '';
  let x = Math.max(1, Math.floor(n));
  for (const [v, s] of table) {
    while (x >= v) {
      out += s;
      x -= v;
    }
  }
  return out;
}

export type TemporalShape =
  | { kind: 'interval'; from: number | null; to: number | null }
  | { kind: 'point'; at: number }
  | { kind: 'undated' };

/**
 * How a statement is dated: validity qualifiers (start/end, or the derived valid_from/valid_to)
 * make an interval with possibly open ends; a point in time makes a point; otherwise undated.
 */
export function temporalOf(s: Pick<KgStatement, 'start' | 'end' | 'point_in_time' | 'valid_from' | 'valid_to'>): TemporalShape {
  const from = yearOf(s.start?.time) ?? null;
  const to = yearOf(s.end?.time) ?? null;
  if (from !== null || to !== null) return { kind: 'interval', from, to };
  const at = yearOf(s.point_in_time?.time);
  if (at !== null) return { kind: 'point', at };
  const vf = yearOf(s.valid_from);
  const vt = yearOf(s.valid_to);
  if (vf !== null || vt !== null) return { kind: 'interval', from: vf, to: vt };
  return { kind: 'undated' };
}

/**
 * Is a statement in force "as of" `year`? Interval: contains the year (open ends open). Point (an
 * event): it has happened by then. Undated: only when undated statements are included — undated is
 * not "always", it is "unknown".
 */
export function inForceAt(t: TemporalShape, year: number | null, includeUndated: boolean): boolean {
  if (year === null) return true;
  if (t.kind === 'undated') return includeUndated;
  if (t.kind === 'point') return t.at <= year;
  return (t.from === null || t.from <= year) && (t.to === null || t.to >= year);
}

export function temporalLabel(t: TemporalShape): string {
  if (t.kind === 'undated') return 'non daté dans la source';
  if (t.kind === 'point') return formatYear(t.at);
  if (t.from !== null && t.to !== null) return t.from === t.to ? formatYear(t.from) : `${formatYear(t.from)} – ${formatYear(t.to)}`;
  if (t.from !== null) return `depuis ${formatYear(t.from)}`;
  return `jusqu’en ${formatYear(t.to)}`;
}

/** Year of a links-layer partial ISO date ("1494-06-07", "-0043", "1822"). */
export function yearOfPartial(s: string | null | undefined): number | null {
  return yearOf(s ?? null);
}
