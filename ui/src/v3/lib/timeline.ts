/**
 * Layout for the "Frise" (timeline): KG timeline events become bars (intervals, possibly with an
 * open end) or points (point in time), grouped in lanes by property. Undated events are listed
 * apart — they are never placed at an invented date.
 */
import type { KgTimelineEvent } from '../api/kg';
import { citeKgStatement, type Citation } from './citations';
import { propertyLabel } from './graph';
import { formatYear, temporalLabel, temporalOf, yearOf, type TemporalShape } from './time';

export interface TimelineItem {
  id: string;
  lane: string;
  title: string;
  other: { qid: string; label: string } | null;
  temporal: TemporalShape;
  /** Inclusive start / end years used for drawing (open ends are clipped to the view and drawn faded). */
  start: number;
  end: number;
  openStart: boolean;
  openEnd: boolean;
  dates: string;
  precision: number | null;
  citation: Citation;
  community: boolean;
}

export interface TimelineModel {
  items: TimelineItem[];
  undated: KgTimelineEvent[];
  lanes: string[];
  min: number;
  max: number;
}

export function buildTimeline(focus: string, events: KgTimelineEvent[], nowYear = new Date().getUTCFullYear()): TimelineModel {
  const items: TimelineItem[] = [];
  const undated: KgTimelineEvent[] = [];
  let min = Infinity;
  let max = -Infinity;
  for (const ev of events) {
    const t = temporalOf(ev);
    const lane = propertyLabel(ev.property);
    const other = ev.subject.qid === focus ? ev.object : ev.subject;
    const valueText = ev.value && typeof ev.value === 'object' ? literalText(ev.value) : '';
    const title = other ? `${lane} : ${other.label}` : valueText ? `${lane} : ${valueText}` : lane;
    if (t.kind === 'undated') {
      const y = yearOf(ev.sort_date);
      if (y === null) {
        undated.push(ev);
        continue;
      }
    }
    let start: number;
    let end: number;
    let openStart = false;
    let openEnd = false;
    if (t.kind === 'point') {
      start = end = t.at;
    } else if (t.kind === 'interval') {
      openStart = t.from === null;
      openEnd = t.to === null;
      start = t.from ?? t.to!;
      end = t.to ?? (t.from !== null ? Math.max(t.from, nowYear) : t.from!);
    } else {
      // time-valued statement (e.g. inception P571) dated by its value.
      start = end = yearOf(ev.sort_date)!;
    }
    min = Math.min(min, start);
    max = Math.max(max, end);
    const cit = citeKgStatement(ev);
    const prec = ev.start?.precision ?? ev.point_in_time?.precision ?? null;
    items.push({
      id: `${ev.provenance.statement_key}:${ev.row_id}`,
      lane,
      title,
      other: other ? { qid: other.qid, label: other.label } : null,
      temporal: t,
      start,
      end,
      openStart,
      openEnd,
      dates: t.kind === 'undated' ? `${formatYear(start)} (date de la valeur)` : temporalLabel(t),
      precision: prec,
      citation: cit,
      community: cit.community,
    });
  }
  items.sort((a, b) => a.start - b.start || a.end - b.end);
  const lanes = [...new Set(items.map((i) => i.lane))];
  if (!Number.isFinite(min)) {
    min = nowYear - 100;
    max = nowYear;
  }
  return { items, undated, lanes, min, max };
}

function literalText(v: Record<string, unknown>): string {
  if (typeof v.amount === 'string') return `${v.amount}${typeof v.unit === 'string' && v.unit !== '1' ? ` ${v.unit}` : ''}`;
  if (typeof v.text === 'string') return v.text;
  if (typeof v.value === 'string') return v.value;
  if (typeof v.time === 'string') return formatYear(yearOf(v.time));
  return '';
}

/** Tick step adapted to the visible span: millennia down to single years. */
export function tickStep(span: number, targetTicks = 8): number {
  const steps = [1, 2, 5, 10, 20, 25, 50, 100, 200, 250, 500, 1000, 2000, 5000];
  for (const s of steps) if (span / s <= targetTicks) return s;
  return 10000;
}

export function ticks(lo: number, hi: number, targetTicks = 8): number[] {
  const step = tickStep(hi - lo, targetTicks);
  const out: number[] = [];
  for (let y = Math.ceil(lo / step) * step; y <= hi; y += step) out.push(y);
  return out;
}

/** Zoom the window [lo, hi] by `factor` (<1 zooms in) around `center`, clamped to [min, max] and ≥ 4 years. */
export function zoomWindow(lo: number, hi: number, factor: number, center: number, min: number, max: number): [number, number] {
  const span = Math.max(4, (hi - lo) * factor);
  const fullSpan = Math.max(4, max - min);
  if (span >= fullSpan) return [min, min + fullSpan];
  const r = (center - lo) / Math.max(1e-9, hi - lo);
  let a = center - span * r;
  let b = a + span;
  if (a < min) {
    a = min;
    b = min + span;
  }
  if (b > max) {
    b = max;
    a = max - span;
  }
  return [a, b];
}

/** Assign rows inside a lane so bars do not overlap (greedy interval packing). */
export function packRows(items: { start: number; end: number }[], minGap: number): number[] {
  const rowsEnd: number[] = [];
  return items.map((it) => {
    let r = rowsEnd.findIndex((e) => it.start > e + minGap);
    if (r === -1) {
      r = rowsEnd.length;
      rowsEnd.push(it.end);
    } else rowsEnd[r] = it.end;
    return r;
  });
}
