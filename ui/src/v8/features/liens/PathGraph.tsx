import { useEffect, useRef, useState } from 'react';
import cytoscape from 'cytoscape';
import { kindStyle } from '../../../v3/lib/kinds';
import type { GraphModel, Tone } from './model';

function token(name: string, fallback: string): string {
  if (typeof document === 'undefined') return fallback;
  const v = getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  return v || fallback;
}

function palette() {
  return {
    fg: token('--v8-fg', '#e8eefc'),
    muted: token('--v8-fg-muted', '#9aa4b2'),
    bg: token('--v8-surface', '#0f1626'),
    border: token('--v8-border-strong', '#5b6b8c'),
    accent: token('--v8-accent', '#4f9dff'),
    claim: token('--v8-scenario', '#ffb74d'),
    estimate: token('--v8-estimate', '#b388ff'),
    community: token('--v8-community', '#9aa4b2'),
    declared: token('--v8-fg-subtle', '#7f8aa0'),
  };
}

const TONE_STYLE: Record<Tone, 'solid' | 'dashed' | 'dotted'> = {
  documented: 'solid',
  community: 'solid',
  claim: 'dashed',
  estimate: 'dashed',
  declared: 'dotted',
  missing: 'dotted',
};

function stylesheet(): cytoscape.StylesheetJson {
  const p = palette();
  return [
    {
      selector: 'node',
      style: {
        'background-color': 'data(color)',
        shape: 'data(shape)' as never,
        label: 'data(label)',
        color: p.fg,
        'font-size': 12,
        'text-valign': 'bottom',
        'text-margin-y': 5,
        'text-outline-color': p.bg,
        'text-outline-width': 2,
        'text-max-width': '150px',
        'text-wrap': 'wrap',
        'min-zoomed-font-size': 4,
        width: 22,
        height: 22,
        'border-width': 1,
        'border-color': p.bg,
      },
    },
    { selector: 'node.virtual', style: { 'border-style': 'dashed', 'border-width': 2, 'border-color': p.estimate } },
    { selector: 'node.end', style: { width: 34, height: 34, 'font-size': 14, 'font-weight': 'bold', 'border-width': 2, 'border-color': p.accent } },
    { selector: 'node.expanded', style: { width: 14, height: 14, 'font-size': 10, color: p.muted } },
    { selector: 'node.sel', style: { 'border-width': 3, 'border-color': p.fg, 'underlay-color': p.accent, 'underlay-opacity': 0.3, 'underlay-padding': 8 } },
    { selector: 'node.dim', style: { opacity: 0.4 } },
    {
      selector: 'edge',
      style: {
        width: 1.4,
        'line-color': p.border,
        'target-arrow-color': p.border,
        'target-arrow-shape': 'triangle',
        'arrow-scale': 0.7,
        'curve-style': 'bezier',
        'line-style': 'data(lineStyle)' as never,
        label: 'data(label)',
        'font-size': 10,
        color: p.muted,
        'text-rotation': 'autorotate',
        'text-outline-color': p.bg,
        'text-outline-width': 2,
        'min-zoomed-font-size': 6,
      },
    },
    { selector: 'edge.claim', style: { 'line-color': p.claim, 'target-arrow-color': p.claim } },
    { selector: 'edge.estimate', style: { 'line-color': p.estimate, 'target-arrow-color': p.estimate } },
    { selector: 'edge.on', style: { width: 3, 'line-color': p.accent, 'target-arrow-color': p.accent, color: p.fg, 'z-index': 9 } },
    { selector: 'edge.on.claim', style: { 'line-color': p.claim, 'target-arrow-color': p.claim } },
    { selector: 'edge.on.estimate', style: { 'line-color': p.estimate, 'target-arrow-color': p.estimate } },
    { selector: 'edge.dim', style: { opacity: 0.25, 'text-opacity': 0 } },
    { selector: 'edge.expanded', style: { width: 1, opacity: 0.55 } },
  ];
}

/**
 * Cytoscape view of the paths: `from` left, `to` right, one row per path, the selected path highlighted (others
 * dimmed), expanded neighbourhoods around their node. Causal claims dashed amber, estimates dashed violet,
 * declared links dotted. Tap a node → onTapNode.
 */
export default function PathGraph({ model, selectedPath, selectedNode, onTapNode, height = 460 }: { model: GraphModel; selectedPath: number; selectedNode: string; onTapNode: (id: string) => void; height?: number }) {
  const el = useRef<HTMLDivElement>(null);
  const cyRef = useRef<cytoscape.Core | null>(null);
  const tap = useRef(onTapNode);
  tap.current = onTapNode;
  const [themeSeq, setThemeSeq] = useState(0);

  useEffect(() => {
    const bump = () => setThemeSeq((s) => s + 1);
    window.addEventListener('v8:theme', bump);
    const mq = typeof window.matchMedia === 'function' ? window.matchMedia('(prefers-color-scheme: dark)') : null;
    mq?.addEventListener?.('change', bump);
    return () => {
      window.removeEventListener('v8:theme', bump);
      mq?.removeEventListener?.('change', bump);
    };
  }, []);

  useEffect(() => {
    if (!el.current) return;
    let cy: cytoscape.Core;
    try {
      cy = cytoscape({ container: el.current, style: stylesheet(), minZoom: 0.15, maxZoom: 2.5, wheelSensitivity: 0.25, boxSelectionEnabled: false, autoungrabify: false });
    } catch {
      return; // no canvas (jsdom): the textual chains remain the primary view
    }
    cy.on('tap', 'node', (evt) => tap.current(String(evt.target.id())));
    cyRef.current = cy;
    return () => {
      cy.destroy();
      cyRef.current = null;
    };
  }, []);

  useEffect(() => {
    cyRef.current?.style(stylesheet());
  }, [themeSeq]);

  useEffect(() => {
    const cy = cyRef.current;
    if (!cy) return;
    cy.batch(() => {
      const want = new Set([...model.nodes.keys(), ...model.edges.keys()]);
      cy.elements().forEach((x) => {
        if (!want.has(x.id())) x.remove();
      });
      for (const n of model.nodes.values()) {
        const ks = kindStyle(n.kind);
        const cls = [n.virtual ? 'virtual' : '', n.role === 'from' || n.role === 'to' ? 'end' : '', n.role === 'expanded' ? 'expanded' : ''].filter(Boolean).join(' ');
        const ex = cy.getElementById(n.id);
        if (ex.nonempty()) {
          ex.data({ label: n.label });
          ex.classes(cls);
          continue;
        }
        cy.add({ group: 'nodes', data: { id: n.id, label: n.label, color: ks.color, shape: ks.shape }, position: { x: n.x, y: n.y }, classes: cls });
      }
      for (const e of model.edges.values()) {
        if (cy.getElementById(e.id).nonempty()) continue;
        if (cy.getElementById(e.source).empty() || cy.getElementById(e.target).empty()) continue;
        cy.add({
          group: 'edges',
          data: { id: e.id, source: e.source, target: e.target, label: e.label, lineStyle: TONE_STYLE[e.tone] },
          classes: [e.tone, e.expanded ? 'expanded' : ''].filter(Boolean).join(' '),
        });
      }
    });
    cy.fit(undefined, 40);
  }, [model]);

  useEffect(() => {
    const cy = cyRef.current;
    if (!cy) return;
    cy.batch(() => {
      cy.elements().removeClass('on dim sel');
      const onPath = new Set<string>();
      for (const e of model.edges.values()) {
        if (e.paths.includes(selectedPath)) {
          cy.getElementById(e.id).addClass('on');
          onPath.add(e.source);
          onPath.add(e.target);
        } else if (!e.expanded) cy.getElementById(e.id).addClass('dim');
      }
      for (const n of model.nodes.values()) {
        if (n.role === 'path' && !onPath.has(n.id)) cy.getElementById(n.id).addClass('dim');
      }
      if (selectedNode) cy.getElementById(selectedNode).addClass('sel');
    });
  }, [model, selectedPath, selectedNode]);

  return <div ref={el} className="lien-graph" style={{ height }} role="img" aria-label="Graphe des chemins trouvés ; la liste des chemins à côté en donne le texte complet." data-testid="lien-graph" />;
}
