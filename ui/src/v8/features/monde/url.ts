/**
 * Monde URL state (every state is a shareable URL):
 *   table, measure, f.<key>=<value>  — X indicator (or `series` = an exact atlas series key)
 *   ytable, ymeasure, yf.<key>       — Y indicator (bivariate)
 *   period, prec, level (adm0|adm1), mode (carte|bivarie), scale (auto|quantile|linear|log|diverging),
 *   proj (exclude|include|only), sel (selected place), logx, logy (1)
 * Accepts the contract link v8Links.series(table, { measure, … }).
 */
import type { Selector } from './api';
import type { ScaleChoice } from './scale';

export interface MondeState {
  x: Selector | null;
  y: Selector | null;
  period: string | null;
  prec: string | null;
  level: 'adm0' | 'adm1';
  mode: 'carte' | 'bivarie';
  scale: ScaleChoice;
  proj: 'exclude' | 'include' | 'only';
  sel: string | null;
  logx: boolean;
  logy: boolean;
}

function selectorFrom(p: URLSearchParams, prefix: '' | 'y'): Selector | null {
  const series = p.get(prefix ? 'yseries' : 'series');
  const table = p.get(`${prefix}table`);
  const measure = p.get(`${prefix}measure`);
  const filters: Record<string, string> = {};
  const fp = `${prefix}f.`;
  for (const [k, v] of p.entries()) if (k.startsWith(fp) && k.length > fp.length) filters[k.slice(fp.length)] = v;
  if (series) return { series };
  const indicator = p.get(`${prefix}indicator`);
  if (table && indicator) return { series: `${table}.${indicator}` };
  if (table && measure !== null) return { table, measure, filters };
  return null;
}

const SCALES = new Set(['auto', 'quantile', 'linear', 'log', 'diverging']);

export function readState(p: URLSearchParams): MondeState {
  const scale = p.get('scale') ?? 'auto';
  const proj = p.get('proj');
  return {
    x: selectorFrom(p, ''),
    y: selectorFrom(p, 'y'),
    period: p.get('period'),
    prec: p.get('prec'),
    level: p.get('level') === 'adm1' ? 'adm1' : 'adm0',
    mode: p.get('mode') === 'bivarie' ? 'bivarie' : 'carte',
    scale: (SCALES.has(scale) ? scale : 'auto') as ScaleChoice,
    proj: proj === 'include' || proj === 'only' ? proj : 'exclude',
    sel: p.get('sel'),
    logx: p.get('logx') === '1',
    logy: p.get('logy') === '1',
  };
}

/** Writes a selector into the params (removing the previous one of the same axis). */
export function writeSelector(p: URLSearchParams, prefix: '' | 'y', s: Selector | null): void {
  for (const k of [...p.keys()]) {
    if (k === `${prefix}table` || k === `${prefix}measure` || k === (prefix ? 'yseries' : 'series') || k.startsWith(`${prefix}f.`)) p.delete(k);
  }
  if (!s) return;
  if (s.table && s.measure !== undefined && s.measure !== null) {
    p.set(`${prefix}table`, s.table);
    p.set(`${prefix}measure`, s.measure);
    for (const [k, v] of Object.entries(s.filters ?? {})) p.set(`${prefix}f.${k}`, v);
  } else if (s.series) p.set(prefix ? 'yseries' : 'series', s.series);
}

export function patchParams(p: URLSearchParams, patch: Record<string, string | null>): URLSearchParams {
  const out = new URLSearchParams(p);
  for (const [k, v] of Object.entries(patch)) {
    if (v === null || v === '') out.delete(k);
    else out.set(k, v);
  }
  return out;
}

/** Selector of a catalog hit or a meta (table + measure + exact filters). */
export function selectorOfSeries(table: string, measure: string, filters: { key: string; value: string }[]): Selector {
  return { table, measure, filters: Object.fromEntries(filters.map((f) => [f.key, f.value])) };
}

export function selectorKey(s: Selector | null): string {
  if (!s) return '';
  if (s.series) return s.series;
  return `${s.table}|${s.measure}|${Object.entries(s.filters ?? {})
    .sort()
    .map(([k, v]) => `${k}=${v}`)
    .join('|')}`;
}

/** Monde URL of a series (used by Comparer, the palette and Fiche links). */
export function mondeHref(s: Selector, extra: Record<string, string> = {}): string {
  const p = new URLSearchParams();
  writeSelector(p, '', s);
  for (const [k, v] of Object.entries(extra)) p.set(k, v);
  return `/v8/monde?${p.toString()}`;
}
