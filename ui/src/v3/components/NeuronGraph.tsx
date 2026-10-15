import { useEffect, useRef } from 'react';
import cytoscape from 'cytoscape';
import { prefersReducedMotion } from '../../lib/useApi';
import { radialPositions, type GraphModel, type Visibility } from '../lib/graph';
import { kindStyle } from '../lib/kinds';

export interface FreshBurst {
  seq: number;
  parent: string;
  nodes: string[];
}

export interface NeuronGraphProps {
  model: GraphModel;
  visibility: Visibility;
  root: string | null;
  selected: string | null;
  burst: FreshBurst | null;
  /** Increment to run a force layout over the whole graph. */
  layoutSeq: number;
  /** Increment to fit the viewport to the graph. */
  fitSeq: number;
  onTapNode: (id: string) => void;
  label: string;
  height?: number;
}

const STYLE: cytoscape.StylesheetJson = [
  {
    selector: 'node',
    style: {
      'background-color': 'data(color)',
      shape: 'data(shape)' as never,
      label: 'data(label)',
      color: '#e8eefc',
      'font-size': 11,
      'text-valign': 'bottom',
      'text-margin-y': 5,
      'text-outline-color': '#0b1020',
      'text-outline-width': 2,
      'text-max-width': '140px',
      'text-wrap': 'ellipsis',
      'min-zoomed-font-size': 7,
      width: 22,
      height: 22,
      'border-width': 1,
      'border-color': '#0b1020',
      'underlay-color': 'data(color)',
      'underlay-padding': 6,
      'underlay-opacity': 0,
      'underlay-shape': 'ellipse',
      'transition-property': 'opacity, background-color, width, height',
      'transition-duration': 250,
    },
  },
  { selector: 'node.expanded', style: { 'border-width': 2, 'border-color': '#e8eefc', width: 28, height: 28 } },
  { selector: 'node.root', style: { width: 38, height: 38, 'font-size': 13, 'font-weight': 'bold', 'underlay-opacity': 0.25, 'underlay-padding': 10 } },
  { selector: 'node.selected', style: { 'border-width': 3, 'border-color': '#ffffff', 'underlay-opacity': 0.45, 'underlay-padding': 12 } },
  { selector: 'node.hl', style: { 'underlay-opacity': 0.18 } },
  { selector: 'node.dim', style: { opacity: 0.12, 'text-opacity': 0 } },
  {
    selector: 'edge',
    style: {
      width: 1.2,
      'line-color': '#5b6b8c',
      'line-opacity': 0.6,
      'curve-style': 'bezier',
      'target-arrow-shape': 'triangle',
      'target-arrow-color': '#5b6b8c',
      'arrow-scale': 0.6,
      label: 'data(label)',
      'font-size': 9,
      color: '#aab7d4',
      'text-rotation': 'autorotate',
      'text-outline-color': '#0b1020',
      'text-outline-width': 2,
      'min-zoomed-font-size': 9,
      'transition-property': 'opacity, line-color',
      'transition-duration': 250,
    },
  },
  { selector: 'edge.undated', style: { 'line-style': 'dotted' } },
  { selector: 'edge.causal', style: { 'line-color': '#ffb74d', 'target-arrow-color': '#ffb74d', 'line-style': 'dashed', width: 2 } },
  { selector: 'edge.hl', style: { 'line-color': '#9fc3ff', 'target-arrow-color': '#9fc3ff', 'line-opacity': 0.95, width: 2, color: '#e8eefc' } },
  { selector: 'edge.dim', style: { opacity: 0.06, 'text-opacity': 0 } },
];

/** Fit `eles` in the viewport without zooming in past 1.25 (a few nodes must not fill the screen). */
function fitCapped(cy: cytoscape.Core, eles: cytoscape.Collection, duration: number) {
  if (eles.empty()) return;
  const bb = eles.boundingBox({});
  const w = cy.width();
  const h = cy.height();
  const pad = 50;
  const z = Math.max(cy.minZoom(), Math.min(1.25, (w - 2 * pad) / Math.max(bb.w, 1), (h - 2 * pad) / Math.max(bb.h, 1)));
  const pan = { x: w / 2 - z * (bb.x1 + bb.w / 2), y: h / 2 - z * (bb.y1 + bb.h / 2) };
  if (duration > 0) cy.animate({ zoom: z, pan }, { duration });
  else {
    cy.zoom(z);
    cy.pan(pan);
  }
}

function nodeData(m: GraphModel, id: string, root: string | null) {
  const n = m.nodes.get(id)!;
  const ks = kindStyle(n.kind);
  return { id, label: n.label, color: ks.color, shape: ks.shape, kind: n.kind ?? '', root: id === root };
}

/**
 * Cytoscape canvas for the Neurones explorer. Imperatively synced with the pure graph model: new
 * nodes burst out of the expanded node (animated unless reduced motion), dated edges outside the
 * "as of" year are dimmed, the selection is highlighted with its neighbourhood.
 */
export default function NeuronGraph({ model, visibility, root, selected, burst, layoutSeq, fitSeq, onTapNode, label, height = 560 }: NeuronGraphProps) {
  const el = useRef<HTMLDivElement>(null);
  const cyRef = useRef<cytoscape.Core | null>(null);
  const tap = useRef(onTapNode);
  tap.current = onTapNode;
  const lastBurst = useRef(0);
  const lastLayout = useRef(0);
  const lastFit = useRef(0);

  useEffect(() => {
    if (!el.current) return;
    let cy: cytoscape.Core;
    try {
      cy = cytoscape({
        container: el.current,
        style: STYLE,
        minZoom: 0.08,
        maxZoom: 3,
        wheelSensitivity: 0.25,
        motionBlur: false,
        textureOnViewport: true,
        hideEdgesOnViewport: true,
        boxSelectionEnabled: false,
      });
    } catch {
      return;
    }
    cy.on('tap', 'node', (evt) => tap.current(String(evt.target.id())));
    cyRef.current = cy;
    return () => {
      cy.destroy();
      cyRef.current = null;
    };
  }, []);

  // Sync elements.
  useEffect(() => {
    const cy = cyRef.current;
    if (!cy) return;
    const reduced = prefersReducedMotion();
    const isBurst = !!burst && burst.seq !== lastBurst.current;
    if (isBurst && cy.$id(burst!.parent).empty() && model.nodes.has(burst!.parent)) {
      // First expansion: the expanded node itself is new; it sits at the centre.
      cy.add({ group: 'nodes', data: nodeData(model, burst!.parent, root), position: { x: 0, y: 0 } });
    }
    const burstSet = new Set(isBurst ? burst!.nodes : []);
    const parent = isBurst ? cy.$id(burst!.parent) : null;
    const center = parent && parent.nonempty() ? parent.position() : { x: 0, y: 0 };
    const fresh = [...model.nodes.keys()].filter((id) => cy.$id(id).empty());
    const targets = radialPositions(center.x, center.y, fresh.length, Math.random() * Math.PI);
    cy.batch(() => {
      fresh.forEach((id, i) => {
        const start = burstSet.has(id) && parent && parent.nonempty() ? { ...center } : targets[i]!;
        cy.add({ group: 'nodes', data: nodeData(model, id, root), position: start });
      });
      for (const [id] of model.nodes) {
        const n = cy.$id(id);
        const d = nodeData(model, id, root);
        if (n.data('label') !== d.label || n.data('color') !== d.color || n.data('shape') !== d.shape) n.data(d);
        n.toggleClass('expanded', !!model.nodes.get(id)?.expanded);
        n.toggleClass('root', id === root);
      }
      for (const [id, e] of model.edges) {
        if (cy.$id(id).nonempty()) continue;
        cy.add({
          group: 'edges',
          data: { id, source: e.source, target: e.target, label: e.causal ? `${e.label} (selon la source)` : e.label },
          classes: [e.temporal.kind === 'undated' ? 'undated' : '', e.causal ? 'causal' : ''].filter(Boolean).join(' '),
        });
      }
      cy.elements().forEach((x) => {
        if (x.isNode() && !model.nodes.has(x.id())) x.remove();
        else if (x.isEdge() && !model.edges.has(x.id())) x.remove();
      });
    });
    fresh.forEach((id, i) => {
      const n = cy.$id(id);
      const target = targets[i]!;
      if (burstSet.has(id) && !reduced) {
        n.style('opacity', 0);
        n.animate({ position: target, style: { opacity: 1 } }, { duration: 650, easing: 'ease-out-cubic', queue: false });
        n.animate({ style: { 'underlay-opacity': 0.7 } }, { duration: 200 }).animate({ style: { 'underlay-opacity': 0 } }, { duration: 1300 });
      } else if (burstSet.has(id)) {
        n.position(target);
      }
    });
    if (isBurst) {
      lastBurst.current = burst!.seq;
      if (parent && parent.nonempty() && !reduced) {
        parent.animate({ style: { 'underlay-opacity': 0.9, 'underlay-padding': 22 } }, { duration: 260 }).animate({ style: { 'underlay-opacity': 0.25, 'underlay-padding': 10 } }, { duration: 1100 });
      }
      if (cy.nodes().length <= 3 + fresh.length && fresh.length > 0) {
        // Fit once the nodes have reached their positions.
        setTimeout(() => cyRef.current && fitCapped(cyRef.current, cyRef.current.elements(), reduced ? 0 : 400), reduced ? 0 : 680);
      }
    }
  }, [model, root, burst]);

  // Visibility ("as of" year) and selection.
  useEffect(() => {
    const cy = cyRef.current;
    if (!cy) return;
    cy.batch(() => {
      cy.nodes().forEach((n) => {
        n.toggleClass('dim', !visibility.nodes.has(n.id()));
      });
      cy.edges().forEach((e) => {
        e.toggleClass('dim', !visibility.edges.has(e.id()));
      });
      cy.elements().removeClass('selected hl');
      if (selected) {
        const n = cy.$id(selected);
        if (n.nonempty()) {
          n.addClass('selected');
          n.connectedEdges().not('.dim').addClass('hl');
          n.neighborhood('node').not('.dim').addClass('hl');
        }
      }
    });
  }, [visibility, selected, model]);

  useEffect(() => {
    const cy = cyRef.current;
    if (!cy || layoutSeq === lastLayout.current) return;
    lastLayout.current = layoutSeq;
    const reduced = prefersReducedMotion();
    cy.layout({
      name: 'cose',
      animate: !reduced && cy.nodes().length < 600,
      animationDuration: 900,
      randomize: false,
      fit: false,
      stop: () => cyRef.current && fitCapped(cyRef.current, cyRef.current.elements(), reduced ? 0 : 300),
      nodeRepulsion: () => 9000,
      idealEdgeLength: () => 110,
      numIter: cy.nodes().length > 800 ? 400 : 1000,
    } as cytoscape.LayoutOptions).run();
  }, [layoutSeq]);

  useEffect(() => {
    const cy = cyRef.current;
    if (!cy || fitSeq === lastFit.current) return;
    lastFit.current = fitSeq;
    const target = selected ? cy.$id(selected).closedNeighborhood() : cy.elements();
    fitCapped(cy, target.nonempty() ? target : cy.elements(), prefersReducedMotion() ? 0 : 450);
  }, [fitSeq, selected]);

  return <div ref={el} className="v3-neuron-canvas" style={{ height }} role="img" aria-label={label} data-testid="neuron-canvas" />;
}
