/**
 * Pure model of the "Neurones" explorer: nodes and edges merged from successive neighbour
 * expansions, and the "as of year" visibility rule. Every edge keeps the statement it came from
 * (with its provenance) so the side panel can cite it.
 */
import type { KgStatement } from '../api/kg';
import { inForceAt, temporalLabel, temporalOf, type TemporalShape } from './time';

export interface GNode {
  id: string;
  label: string;
  kind: string | null;
  /** Expanded = its neighbours were fetched (at least one page). */
  expanded: boolean;
  /** Cursor for the next page of neighbours, when the API said there are more. */
  nextCursor: string | null;
  /** Order of discovery (0 = root). */
  order: number;
}

export interface GEdge {
  id: string;
  source: string;
  target: string;
  pid: string;
  label: string;
  temporal: TemporalShape;
  dates: string;
  causal: boolean;
  statement: KgStatement;
}

export interface GraphModel {
  nodes: Map<string, GNode>;
  edges: Map<string, GEdge>;
}

export const emptyGraph = (): GraphModel => ({ nodes: new Map(), edges: new Map() });

export function propertyLabel(p: { pid: string; label_fr: string | null; label_en: string | null }): string {
  return p.label_fr || p.label_en || p.pid;
}

export function edgeId(st: KgStatement): string {
  return `st:${st.provenance.statement_key || st.row_id}`;
}

export interface MergeResult {
  graph: GraphModel;
  addedNodes: string[];
  addedEdges: string[];
}

/**
 * Merge one page of neighbours of `center` into the graph (immutable: returns new maps).
 * Literal-valued statements (no object) are not graph edges and are skipped here; the entity card
 * lists them.
 */
export function mergeNeighbours(
  g: GraphModel,
  center: { qid: string; label: string; kind?: string | null },
  edges: KgStatement[],
  nextCursor: string | null,
): MergeResult {
  const nodes = new Map(g.nodes);
  const out = new Map(g.edges);
  const addedNodes: string[] = [];
  const addedEdges: string[] = [];
  let order = nodes.size;
  const prev = nodes.get(center.qid);
  nodes.set(center.qid, {
    id: center.qid,
    label: prev?.label && prev.label !== prev.id ? prev.label : center.label,
    kind: prev?.kind ?? center.kind ?? null,
    expanded: true,
    nextCursor,
    order: prev?.order ?? order++,
  });
  if (!prev) addedNodes.push(center.qid);
  for (const st of edges) {
    if (!st.object) continue;
    const ends = [st.subject, st.object];
    for (const e of ends) {
      if (!nodes.has(e.qid)) {
        nodes.set(e.qid, { id: e.qid, label: e.label, kind: null, expanded: false, nextCursor: null, order: order++ });
        addedNodes.push(e.qid);
      }
    }
    const id = edgeId(st);
    if (out.has(id)) continue;
    const temporal = temporalOf(st);
    out.set(id, {
      id,
      source: st.subject.qid,
      target: st.object.qid,
      pid: st.property.pid,
      label: propertyLabel(st.property),
      temporal,
      dates: temporalLabel(temporal),
      causal: st.property.causal_assertion,
      statement: st,
    });
    addedEdges.push(id);
  }
  return { graph: { nodes, edges: out }, addedNodes, addedEdges };
}

export function setNodeKind(g: GraphModel, id: string, kind: string | null, label?: string): GraphModel {
  const n = g.nodes.get(id);
  if (!n || (n.kind === kind && (!label || label === n.label))) return g;
  const nodes = new Map(g.nodes);
  nodes.set(id, { ...n, kind, label: label || n.label });
  return { nodes, edges: g.edges };
}

export interface Visibility {
  edges: Set<string>;
  nodes: Set<string>;
}

/**
 * Edges in force "as of" `year` (null = no filter); a node is visible when it is the root, has
 * been expanded, or touches a visible edge. Hidden elements are dimmed, not deleted.
 */
export function visibleAt(g: GraphModel, year: number | null, includeUndated: boolean, root: string | null): Visibility {
  const edges = new Set<string>();
  const nodes = new Set<string>();
  if (root) nodes.add(root);
  for (const e of g.edges.values()) {
    if (inForceAt(e.temporal, year, includeUndated)) {
      edges.add(e.id);
      nodes.add(e.source);
      nodes.add(e.target);
    }
  }
  for (const n of g.nodes.values()) if (n.expanded) nodes.add(n.id);
  return { edges, nodes };
}

/** Edges touching a node, outgoing first, then by property label. */
export function incidentEdges(g: GraphModel, id: string): GEdge[] {
  const out: GEdge[] = [];
  for (const e of g.edges.values()) if (e.source === id || e.target === id) out.push(e);
  return out.sort((a, b) => Number(b.source === id) - Number(a.source === id) || a.label.localeCompare(b.label, 'fr'));
}

/** Range of years mentioned by dated edges (for the time slider). */
export function yearRange(g: GraphModel): [number, number] | null {
  let lo = Infinity;
  let hi = -Infinity;
  for (const e of g.edges.values()) {
    const t = e.temporal;
    const ys = t.kind === 'point' ? [t.at] : t.kind === 'interval' ? [t.from, t.to].filter((x): x is number => x !== null) : [];
    for (const y of ys) {
      lo = Math.min(lo, y);
      hi = Math.max(hi, y);
    }
  }
  return Number.isFinite(lo) ? [lo, hi] : null;
}

/** Radial target positions for `count` new nodes around (cx, cy), avoiding the first ring when crowded. */
export function radialPositions(cx: number, cy: number, count: number, startAngle = 0): { x: number; y: number }[] {
  const out: { x: number; y: number }[] = [];
  const perRing = 14;
  for (let i = 0; i < count; i++) {
    const ring = Math.floor(i / perRing);
    const inRing = Math.min(perRing, count - ring * perRing);
    const r = 150 + ring * 90;
    const a = startAngle + ((i % perRing) / inRing) * Math.PI * 2 + ring * 0.2;
    out.push({ x: cx + r * Math.cos(a), y: cy + r * Math.sin(a) });
  }
  return out;
}
