/** Pure helpers of the decision studio (no React): number formatting, graph layout, tornado geometry. */
import type { GraphEdge, GraphNode, HeadlineRow, SensVar, Value } from './api';

const NF3 = new Intl.NumberFormat('fr-FR', { maximumSignificantDigits: 3 });

/** French number, 3 significant digits; null/undefined → null (callers render a typed Missing, never 0). */
export function num(x: number | null | undefined, signed = false): string | null {
  if (x === null || x === undefined || !Number.isFinite(x)) return null;
  const s = NF3.format(x).replace('-', '−');
  return signed && x > 0 ? `+${s}` : s;
}

const UNIT_FR: Record<string, string> = {
  percent: '%',
  pp: 'pt de %',
  log_points: 'pts log',
  pp_gdp: 'pt de PIB',
  occurrence: 'survenue',
};
export function unitFr(u: string | null | undefined): string {
  if (!u) return '';
  if (u in UNIT_FR) return UNIT_FR[u]!;
  if (u.startsWith('level:')) return u.slice(6);
  return u.replace(/_/g, ' ');
}

export function valueText(v: Value | undefined, unit: string): string | null {
  if (!v || v.p50 == null) return null;
  const n = num(v.p50, true);
  return n ? `${n} ${unitFr(unit)}`.trim() : null;
}

export function rangeText(v: Value | undefined): string | null {
  if (!v || v.p05 == null || v.p95 == null) return null;
  if (v.degenerate) return 'point (pas d’incertitude publiée)';
  return `[${num(v.p05, true)} ; ${num(v.p95, true)}]`;
}

/** Groups of headline rows by variable concept (rows = concepts, columns = countries) for the dense matrix. */
export function matrix(rows: HeadlineRow[]): { concepts: { key: string; label: string; unit: string }[]; geos: string[]; cell: (concept: string, geo: string) => HeadlineRow | undefined } {
  const concepts: { key: string; label: string; unit: string }[] = [];
  const geos: string[] = [];
  const map = new Map<string, HeadlineRow>();
  for (const r of rows) {
    const key = r.id.slice(0, r.id.lastIndexOf('@'));
    if (!concepts.some((c) => c.key === key)) concepts.push({ key, label: r.label_fr, unit: r.unit });
    if (!geos.includes(r.geo)) geos.push(r.geo);
    map.set(`${key}@${r.geo}`, r);
  }
  return { concepts, geos, cell: (c, g) => map.get(`${c}@${g}`) };
}

// ------------------------------------------------------------------ graph layout
export interface Placed {
  node: GraphNode;
  x: number;
  y: number;
  col: number;
}

/** Column of a node: decision 0, direct changes 1, then by path length; stops / plugins / ties after their source. */
export function layout(nodes: GraphNode[], edges: GraphEdge[], colW = 300, rowH = 80): Placed[] {
  const col = new Map<string, number>();
  col.set('decision', 0);
  const incoming = new Map<string, string[]>();
  for (const e of edges) incoming.set(e.to, [...(incoming.get(e.to) ?? []), e.from]);
  const colOf = (id: string, seen = new Set<string>()): number => {
    const known = col.get(id);
    if (known !== undefined) return known;
    if (seen.has(id)) return 1;
    seen.add(id);
    const from = incoming.get(id) ?? [];
    const c = from.length ? Math.max(...from.map((f) => colOf(f, seen))) + 1 : 1;
    col.set(id, c);
    return c;
  };
  for (const n of nodes) colOf(n.id);
  const byCol = new Map<number, GraphNode[]>();
  for (const n of nodes) {
    const c = col.get(n.id) ?? 1;
    byCol.set(c, [...(byCol.get(c) ?? []), n]);
  }
  const maxRows = Math.max(1, ...[...byCol.values()].map((l) => l.length));
  const out: Placed[] = [];
  for (const [c, list] of byCol) {
    const off = ((maxRows - list.length) * rowH) / 2;
    list.forEach((n, i) => out.push({ node: n, col: c, x: c * colW, y: off + i * rowH }));
  }
  return out;
}

/** Nodes and edges for one geography (plus world-level and decision nodes); "*" keeps everything. */
export function filterGeo(nodes: GraphNode[], edges: GraphEdge[], geo: string): { nodes: GraphNode[]; edges: GraphEdge[] } {
  if (geo === '*' || !geo) return { nodes, edges };
  const keep = new Set(nodes.filter((n) => n.kind === 'decision' || n.geo === geo || n.geo === 'WLD' || n.geo === '').map((n) => n.id));
  const es = edges.filter((e) => keep.has(e.from) && keep.has(e.to));
  return { nodes: nodes.filter((n) => keep.has(n.id)), edges: es };
}

// ------------------------------------------------------------------ tornado
export interface TornadoBar {
  key: string;
  label: string;
  lo: number;
  hi: number;
  r2: number | null;
}

export function tornado(v: SensVar, period: string): { center: number | null; bars: TornadoBar[]; flat: { key: string; label: string }[]; missing: { key: string; label: string }[] } {
  const center = v.center[period] ?? null;
  const bars: TornadoBar[] = [];
  const missing: { key: string; label: string }[] = [];
  for (const b of v.bars) {
    const lo = b.low[period];
    const hi = b.high[period];
    if (lo == null || hi == null) {
      missing.push({ key: b.estimate_key, label: b.short });
      continue;
    }
    bars.push({ key: b.estimate_key, label: b.short, lo: Math.min(lo, hi), hi: Math.max(lo, hi), r2: b.mc_r2[period] ?? null });
  }
  bars.sort((a, b) => b.hi - b.lo - (a.hi - a.lo));
  return { center, bars, flat: v.no_uncertainty.map((f) => ({ key: f.estimate_key, label: f.short })), missing };
}

/** Linear scale over a domain that always contains 0 and the center. */
export function scale(values: number[], width: number, pad = 8): { x: (v: number) => number; lo: number; hi: number } {
  const lo = Math.min(0, ...values);
  const hi = Math.max(0, ...values);
  const span = hi - lo || 1;
  return { x: (v: number) => pad + ((v - lo) * (width - 2 * pad)) / span, lo, hi };
}

export const TAB_IDS = ['effets', 'propagation', 'sensibilite', 'comparer', 'pourquoi', 'brief'] as const;
export type TabId = (typeof TAB_IDS)[number];
