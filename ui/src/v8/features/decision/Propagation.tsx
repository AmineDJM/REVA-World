import { useEffect, useMemo, useRef, useState } from 'react';
import type { Core } from 'cytoscape';
import { Badge, EmptyState } from '../../shell/ui';
import type { GraphEdge, GraphNode, RunBundle } from './api';
import { EdgeEvidence } from './Evidence';
import { filterGeo, layout, num } from './model';

function token(name: string, fallback: string): string {
  try {
    return getComputedStyle(document.documentElement).getPropertyValue(name).trim() || fallback;
  } catch {
    return fallback;
  }
}

function nodeText(n: GraphNode): string {
  const v = n.value && n.value.p50 != null ? `\n${num(n.value.p50, true)}${n.value.degenerate ? ' (point)' : ''}` : '';
  return `${n.label}${v}`;
}

/** Cytoscape canvas (lazy-loaded); the edge table below is the keyboard / screen-reader equivalent. */
function GraphCanvas({ nodes, edges, selected, onSelect }: { nodes: GraphNode[]; edges: GraphEdge[]; selected: string | null; onSelect: (id: string) => void }) {
  const box = useRef<HTMLDivElement | null>(null);
  const cyRef = useRef<Core | null>(null);
  const [failed, setFailed] = useState(false);
  const [theme, setTheme] = useState(0);
  useEffect(() => {
    const f = () => setTheme((t) => t + 1);
    window.addEventListener('v8:theme', f);
    return () => window.removeEventListener('v8:theme', f);
  }, []);
  useEffect(() => {
    let dead = false;
    const placed = layout(nodes, edges);
    import('cytoscape')
      .then(({ default: cytoscape }) => {
        if (dead || !box.current) return;
        const c = {
          fg: token('--v8-fg', '#15171c'),
          muted: token('--v8-fg-muted', '#565d6a'),
          surface: token('--v8-surface', '#fff'),
          border: token('--v8-border-strong', '#c8ccd3'),
          accent: token('--v8-accent', '#3653d3'),
          scen: token('--v8-scenario', '#b35400'),
          scenBg: token('--v8-scenario-bg', '#fdf0e1'),
          est: token('--v8-estimate', '#0b7a70'),
          estBg: token('--v8-estimate-bg', '#e2f4f1'),
          miss: token('--v8-missing', '#7a5a1c'),
          missBg: token('--v8-missing-bg', '#faf5ea'),
          warn: token('--v8-warn', '#9a6700'),
          judged: token('--v8-judged', '#5546b8'),
          judgedBg: token('--v8-judged-bg', '#ecebfa'),
        };
        cyRef.current?.destroy();
        const cy = cytoscape({
          container: box.current,
          elements: [
            ...placed.map((p) => ({ data: { id: p.node.id, label: nodeText(p.node), kind: p.node.kind }, position: { x: p.x, y: p.y } })),
            ...edges.map((e) => ({ data: { id: `e:${e.id}`, source: e.from, target: e.to, label: e.kind === 'estimate' ? `${e.source_short ?? ''}\n${e.effect?.beta_label_fr ?? ''}` : e.label, kind: e.kind, edge: e.id } })),
          ],
          layout: { name: 'preset', fit: true, padding: 24 },
          minZoom: 0.25,
          maxZoom: 2.5,
          wheelSensitivity: 0.3,
          style: [
            { selector: 'node', style: { shape: 'round-rectangle', width: 168, height: 46, 'background-color': c.surface, 'border-width': 1.5, 'border-color': c.border, label: 'data(label)', 'text-wrap': 'wrap', 'text-max-width': '156px', 'font-size': 10.5, color: c.fg, 'text-valign': 'center', 'text-halign': 'center' } },
            { selector: 'node[kind = "decision"]', style: { 'background-color': c.accent, color: '#fff', 'border-width': 0, 'font-weight': 'bold' } },
            { selector: 'node[kind = "direct"]', style: { 'background-color': c.scenBg, 'border-color': c.scen, 'border-style': 'dashed' } },
            { selector: 'node[kind = "variable"]', style: { 'background-color': c.estBg, 'border-color': c.est } },
            { selector: 'node[kind = "plugin"]', style: { 'background-color': c.judgedBg, 'border-color': c.judged } },
            { selector: 'node[kind = "stop"], node[kind = "unquantified"]', style: { 'background-color': c.missBg, 'border-color': c.miss, 'border-style': 'dotted', color: c.miss } },
            { selector: 'node[kind = "tie"]', style: { 'background-color': c.missBg, 'border-color': c.warn, 'border-style': 'double', 'border-width': 3 } },
            { selector: 'edge', style: { width: 1.6, 'curve-style': 'bezier', 'line-color': c.border, 'target-arrow-color': c.border, 'target-arrow-shape': 'triangle', 'arrow-scale': 0.8, label: 'data(label)', 'font-size': 9, 'text-wrap': 'wrap', color: c.muted, 'text-background-color': c.surface, 'text-background-opacity': 0.9, 'text-background-padding': '2px' } },
            { selector: 'edge[kind = "estimate"]', style: { 'line-color': c.est, 'target-arrow-color': c.est, width: 2.2 } },
            { selector: 'edge[kind = "assumption"]', style: { 'line-color': c.scen, 'target-arrow-color': c.scen, 'line-style': 'dashed' } },
            { selector: 'edge[kind = "missing"], edge[kind = "sign_only"]', style: { 'line-color': c.miss, 'target-arrow-color': c.miss, 'line-style': 'dotted' } },
            { selector: 'edge[kind = "tie"]', style: { 'line-color': c.warn, 'target-arrow-color': c.warn, 'line-style': 'dashed' } },
            { selector: '.sel', style: { 'line-color': c.accent, 'target-arrow-color': c.accent, width: 4, 'z-index': 9 } },
          ],
        });
        cy.on('tap', 'edge', (ev) => onSelect(String(ev.target.data('edge'))));
        cy.on('tap', 'node', (ev) => {
          const id = String(ev.target.id());
          const into = edges.find((e) => e.to === id);
          if (into) onSelect(into.id);
        });
        // readability over fitting: never smaller than 0.72; start at the decision (pan / pinch for the rest)
        const MINZ = 0.72;
        if (cy.zoom() < MINZ && box.current) {
          const root = cy.$id('decision');
          const pos = root.position();
          cy.zoom(MINZ);
          cy.pan({ x: 16 - (pos.x - 84) * MINZ, y: box.current.clientHeight / 2 - pos.y * MINZ });
        }
        cyRef.current = cy;
        if (selected) cy.$id(`e:${selected}`).addClass('sel');
      })
      .catch(() => setFailed(true));
    return () => {
      dead = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [nodes, edges, theme]);
  useEffect(() => {
    const cy = cyRef.current;
    if (!cy) return;
    cy.$('.sel').removeClass('sel');
    if (selected) cy.$id(`e:${selected}`).addClass('sel');
  }, [selected]);
  useEffect(() => () => cyRef.current?.destroy(), []);
  if (failed) return <p className="v8-muted">Graphe indisponible dans ce navigateur : la liste des liens ci-dessous donne la même information.</p>;
  const h = Math.min(560, Math.max(300, 70 * Math.max(...layout(nodes, edges).map((p) => p.y / 76 + 1), 3)));
  return <div ref={box} className="v8d-canvas" style={{ height: h }} role="img" aria-label="Graphe de propagation : décision, changements directs, variables atteintes, liens manquants" />;
}

const KIND_FR: Record<GraphEdge['kind'], string> = {
  assumption: 'hypothèse',
  estimate: 'estimation',
  plugin: 'modèle',
  model_output: 'sortie',
  missing: 'manquant',
  tie: 'égalité',
  sign_only: 'signe',
};

export function Propagation({ bundle, geo, onGeo, edgeId, onEdge }: { bundle: RunBundle; geo: string; onGeo: (g: string) => void; edgeId: string | null; onEdge: (id: string) => void }) {
  const geos = bundle.graph.geos.filter((g) => g !== 'WLD');
  const g = geo || (geos.length > 1 ? geos[0]! : '*');
  const { nodes, edges } = useMemo(() => filterGeo(bundle.graph.nodes, bundle.graph.edges, g), [bundle, g]);
  const sel = bundle.graph.edges.find((e) => e.id === edgeId) ?? edges.find((e) => e.kind === 'estimate' || e.kind === 'tie' || e.kind === 'missing') ?? null;
  const label = (id: string) => bundle.graph.nodes.find((n) => n.id === id)?.label ?? id;
  if (bundle.graph.nodes.length <= 1) return <EmptyState title="Aucun lien">La décision n’a produit aucun changement direct.</EmptyState>;
  return (
    <div className="v8d-prop">
      <div className="v8d-prop-main">
        {geos.length > 1 && (
          <div className="v8d-chips" role="group" aria-label="Pays affiché dans le graphe">
            {[...geos, '*'].map((x) => (
              <button key={x} type="button" className={`v8d-chip${x === g ? ' is-on' : ''}`} aria-pressed={x === g} onClick={() => onGeo(x)}>
                {x === '*' ? 'tous' : x}
              </button>
            ))}
          </div>
        )}
        <GraphCanvas nodes={nodes} edges={edges} selected={sel?.id ?? null} onSelect={onEdge} />
        <div className="v8d-legend v8-row">
          <Badge kind="scenario">hypothèse énoncée</Badge>
          <Badge kind="estimate">estimation attribuée</Badge>
          <Badge kind="judged">modèle REVA</Badge>
          <Badge kind="missing">lien manquant / égalité</Badge>
        </div>
        <div className="v8d-table-box">
          <table className="v8d-edges" aria-label="Liens de propagation (cliquer pour la preuve)">
            <thead>
              <tr>
                <th scope="col">De → vers</th>
                <th scope="col">Lien</th>
                <th scope="col">Effet attribué</th>
                <th scope="col">Source · méthode</th>
              </tr>
            </thead>
            <tbody>
              {edges.map((e) => (
                <tr key={e.id} className={sel?.id === e.id ? 'is-sel' : undefined}>
                  <td>
                    <button type="button" className="v8d-linkbtn" onClick={() => onEdge(e.id)} aria-pressed={sel?.id === e.id}>
                      {label(e.from)} → {label(e.to)}
                    </button>
                  </td>
                  <td>
                    <span className={`v8d-kind v8d-kind-${e.kind}`}>{KIND_FR[e.kind]}</span>
                    {(e.n_alternatives ?? 0) > 0 && <span className="v8d-alt"> +{e.n_alternatives} concurrente(s)</span>}
                  </td>
                  <td className="v8-num">{e.kind === 'estimate' ? `${e.effect?.published_fr ?? e.effect?.beta_label_fr ?? ''}` : e.label}</td>
                  <td>{e.source_short ? `${e.source_short}${e.method ? ` · ${e.method}` : ''}` : '—'}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>
      <aside className="v8d-prop-side" aria-label="Preuve du lien sélectionné">
        <h3 className="v8d-side-title">Preuve</h3>
        {sel ? <EdgeEvidence edge={sel} bundle={bundle} /> : <p className="v8-muted">Sélectionnez un lien.</p>}
      </aside>
    </div>
  );
}
