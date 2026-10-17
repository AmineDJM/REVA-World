/**
 * Pure model of the Liens page: URL state, graph model (paths + expansions) with a deterministic layered
 * layout, and the French wording of the answer and of each hop's evidence.
 */
import type { EgoData, Hop, NodeRef, PathOut, PathsData } from './api';

// ------------------------------------------------------------------ URL state
export interface LiensState {
  from: string;
  to: string;
  families: string[];
  causal: boolean;
  hops: number;
  /** selected path, 0-based */
  path: number;
  /** selected node id */
  node: string;
}

export const DEFAULT_HOPS = 4;
export const HOP_CHOICES = [2, 3, 4, 5, 6];

export function parseState(p: URLSearchParams): LiensState {
  const hops = Number(p.get('hops'));
  const path = Number(p.get('p'));
  return {
    from: p.get('from') ?? '',
    to: p.get('to') ?? '',
    families: (p.get('fam') ?? '')
      .split(',')
      .map((x) => x.trim())
      .filter((x) => /^[a-z][a-z_]{1,31}$/.test(x))
      .sort(),
    causal: p.get('causal') === '1',
    hops: HOP_CHOICES.includes(hops) ? hops : DEFAULT_HOPS,
    path: Number.isInteger(path) && path > 0 ? path - 1 : 0,
    node: p.get('n') ?? '',
  };
}

/** Query string for a state; defaults are omitted (one URL per state, short). `from`/`to` first, as v8Links.path. */
export function stateQuery(s: LiensState): URLSearchParams {
  const q = new URLSearchParams();
  if (s.from) q.set('from', s.from);
  if (s.to) q.set('to', s.to);
  if (s.families.length) q.set('fam', [...s.families].sort().join(','));
  if (s.causal) q.set('causal', '1');
  if (s.hops !== DEFAULT_HOPS) q.set('hops', String(s.hops));
  if (s.path > 0) q.set('p', String(s.path + 1));
  if (s.node) q.set('n', s.node);
  return q;
}

// ------------------------------------------------------------------ wording
export const KIND_FR: Record<Hop['kind'], string> = {
  statement: 'lien documenté',
  causal_claim: 'affirmation causale',
  effect_estimate: 'estimation d’effet',
  estimate_scope: 'périmètre d’estimation',
  declared_bridge: 'lien d’identifiant déclaré',
};

export const PATH_KIND_FR: Record<PathOut['kind'], string> = {
  documented: 'liens documentés',
  mixed: 'liens documentés + affirmations attribuées',
  causal: 'affirmations attribuées',
};

export function nodeLabel(nodes: Record<string, NodeRef>, id: string): string {
  return nodes[id]?.label ?? id;
}

export function plural(n: number, one: string, many: string): string {
  return `${n.toLocaleString('fr-FR')} ${n > 1 ? many : one}`;
}

/** The answer, first: how many paths, the shortest, its kind. */
export function answerSentence(d: PathsData): string {
  const a = d.from.label;
  const b = d.to.label;
  if (!d.found || !d.paths.length) return `Aucun chemin trouvé entre ${a} et ${b}.`;
  const shortest = Math.min(...d.paths.map((p) => p.length));
  const best = d.paths[0]!;
  const via = best.nodes.slice(1, -1).map((n) => nodeLabel(d.nodes, n));
  const viaTxt = via.length ? ` via ${via.slice(0, 2).join(', ')}${via.length > 2 ? '…' : ''}` : ' directement';
  const causal = d.paths.filter((p) => p.kind !== 'documented').length;
  return (
    `${a} et ${b} sont reliés par ${plural(d.paths.length, 'chemin', 'chemins')} ` +
    `(le plus court : ${plural(shortest, 'lien', 'liens')}). Meilleur chemin${viaTxt}` +
    (causal ? ` ; ${causal} chemin${causal > 1 ? 's' : ''} passe${causal > 1 ? 'nt' : ''} par des affirmations attribuées.` : '.')
  );
}

export type Tone = 'documented' | 'community' | 'claim' | 'estimate' | 'declared' | 'missing';

export function hopTone(h: Hop): Tone {
  if (h.strength?.level === 'missing') return 'missing';
  if (h.kind === 'causal_claim') return 'claim';
  if (h.kind === 'effect_estimate') return 'estimate';
  if (h.kind === 'estimate_scope' || h.kind === 'declared_bridge') return 'declared';
  if (h.strength?.verification === 'community_edited_not_verified') return 'community';
  return 'documented';
}

/** Short evidence badge text: "note B · T1", "estimation · var", "Wikidata", "source". */
export function strengthShort(h: Hop): string {
  const s = h.strength;
  if (!s) return 'force inconnue';
  if (s.level === 'missing') return 'introuvable';
  if (h.kind === 'causal_claim') return `note ${s.grade ?? '—'}${s.tier ? ` · ${s.tier}` : ''}`;
  if (h.kind === 'effect_estimate') return `estimation${s.method ? ` · ${s.method}` : ''}`;
  if (h.kind === 'estimate_scope') return 'périmètre déclaré';
  if (h.kind === 'declared_bridge') return 'identifiant déclaré';
  if (s.verification === 'community_edited_not_verified') return 'Wikidata · non vérifié';
  if (s.verification === 'derived') return 'dérivé (REVA)';
  return 'déclaré par la source';
}

export function licenceShort(h: Hop): { text: string; restricted: boolean; unknown: boolean } {
  const l = h.licence;
  if (!l) return { text: h.kind === 'declared_bridge' ? 'registre REVA' : 'licence inconnue', restricted: false, unknown: h.kind !== 'declared_bridge' };
  const nc = l.non_commercial || l.commercial_use === 'not_permitted';
  return { text: l.licence.length > 28 ? `${l.licence.slice(0, 26)}…` : l.licence, restricted: Boolean(l.restricted || nc), unknown: false };
}

// ------------------------------------------------------------------ graph model
export interface GNode {
  id: string;
  label: string;
  kind: string | null;
  virtual: boolean;
  x: number;
  y: number;
  role: 'from' | 'to' | 'path' | 'expanded';
  paths: number[];
}

export interface GEdge {
  id: string;
  source: string;
  target: string;
  label: string;
  tone: Tone;
  paths: number[];
  expanded: boolean;
}

export interface GraphModel {
  nodes: Map<string, GNode>;
  edges: Map<string, GEdge>;
}

const STEP = 170;
const ROW = 74;

function edgeKey(h: Hop): string {
  return `${h.from}→${h.to}:${h.kind}:${h.property?.pid ?? h.relation?.fr ?? ''}`;
}

/** Layered layout: `from` at x = 0, `to` at the right, every path on its own row, shared nodes placed once. */
export function buildGraph(d: PathsData | null, expansions: { center: string; data: EgoData }[] = []): GraphModel {
  const nodes = new Map<string, GNode>();
  const edges = new Map<string, GEdge>();
  if (d) {
    const maxLen = Math.max(1, ...d.paths.map((p) => p.length));
    const rows = d.paths.length;
    const add = (id: string, x: number, y: number, role: GNode['role'], pi: number | null) => {
      const n = nodes.get(id);
      if (n) {
        if (pi !== null && !n.paths.includes(pi)) n.paths.push(pi);
        return;
      }
      const r = d.nodes[id];
      nodes.set(id, { id, label: r?.label ?? id, kind: r?.kind ?? null, virtual: r?.virtual ?? false, x, y, role, paths: pi === null ? [] : [pi] });
    };
    add(d.from.id, 0, 0, 'from', null);
    add(d.to.id, maxLen * STEP, 0, 'to', null);
    d.paths.forEach((p, pi) => {
      const y = (pi - (rows - 1) / 2) * ROW;
      p.nodes.forEach((id, i) => {
        if (i === 0 || i === p.nodes.length - 1) {
          add(id, i === 0 ? 0 : maxLen * STEP, 0, i === 0 ? 'from' : 'to', pi);
          return;
        }
        add(id, (i * maxLen * STEP) / p.length, y, 'path', pi);
      });
      for (const h of p.hops) {
        const k = edgeKey(h);
        const e = edges.get(k);
        if (e) {
          if (!e.paths.includes(pi)) e.paths.push(pi);
          continue;
        }
        edges.set(k, { id: k, source: h.from, target: h.to, label: hopEdgeLabel(h), tone: hopTone(h), paths: [pi], expanded: false });
      }
    });
  }
  for (const { center, data } of expansions) {
    let c = nodes.get(center);
    if (!c) {
      c = { id: center, label: data.center.label, kind: data.center.kind, virtual: data.center.virtual, x: 0, y: 0, role: 'expanded', paths: [] };
      nodes.set(center, c);
    }
    const fresh = data.edges.filter((e) => !nodes.has(e.hop.to));
    const n = Math.max(fresh.length, 1);
    const radius = 120 + Math.min(80, n * 4);
    fresh.forEach((e, i) => {
      const ang = (2 * Math.PI * i) / n - Math.PI / 2;
      const r = data.nodes[e.hop.to];
      nodes.set(e.hop.to, {
        id: e.hop.to,
        label: r?.label ?? e.hop.to,
        kind: r?.kind ?? null,
        virtual: r?.virtual ?? false,
        x: c!.x + radius * Math.cos(ang),
        y: c!.y + radius * Math.sin(ang),
        role: 'expanded',
        paths: [],
      });
    });
    for (const e of data.edges) {
      const k = edgeKey(e.hop);
      if (!edges.has(k)) edges.set(k, { id: k, source: e.hop.from, target: e.hop.to, label: hopEdgeLabel(e.hop), tone: hopTone(e.hop), paths: [], expanded: true });
    }
  }
  return { nodes, edges };
}

export function hopEdgeLabel(h: Hop): string {
  if (h.kind === 'causal_claim') return `${h.relation?.fr ?? 'cause'} (${h.strength?.grade ?? '—'})`;
  return h.family?.label.fr ?? h.relation?.fr ?? KIND_FR[h.kind];
}

/** The hop licence in the shape SourceChip reads (nulls dropped). */
export function toRights(l: Hop['licence']): import('../../../v3/lib/licence').RightsLike | null {
  if (!l) return null;
  return {
    licence: l.licence,
    commercial_use: l.commercial_use ?? undefined,
    non_commercial: l.non_commercial ?? undefined,
    share_alike: l.share_alike ?? undefined,
    no_derivatives: l.no_derivatives ?? undefined,
    open_licence: l.open_licence ?? null,
    restricted: l.restricted ?? undefined,
    attribution: l.attribution,
    licence_url: l.licence_url,
    required_notice: l.required_notice,
  };
}

/** Server warnings in French; the honesty note (path ≠ cause, claims ≠ causation) and truncation are shown elsewhere. */
export function warningsFr(ws: { code: string; message: string }[], d: PathsData): string[] {
  const out: string[] = [];
  for (const w of ws) {
    let t: string | null;
    if (w.code === 'path_not_causal' || w.code === 'claims_not_causation' || w.code === 'truncated') t = null;
    else if (w.code === 'community_edited') t = 'Certains liens viennent de Wikidata : contributions communautaires, non vérifiées par REVA.';
    else if (w.code === 'licence_restricted') {
      const ls = [...new Set(d.paths.flatMap((p) => p.restricted_licences))];
      t = `Licences restrictives à respecter pour réutiliser ces liens : ${(ls.join(' ; ') || w.message).replace(/\.+$/, '')}.`;
    } else if (w.code === 'fixture_data') t = 'Contient des données de test (test_fixture), pas des données publiées.';
    else t = w.message;
    if (t && !out.includes(t)) out.push(t);
  }
  return out;
}
