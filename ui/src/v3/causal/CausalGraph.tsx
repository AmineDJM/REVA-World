import { useEffect, useRef } from 'react';
import cytoscape from 'cytoscape';
import { prefersReducedMotion } from '../../lib/useApi';
import { FAMILIES, FAMILY_STYLE, GRADE_STYLE, type CausalGraphModel } from './model';
import { GRADES } from './api';

const familyStyles: cytoscape.StylesheetJson = FAMILIES.map((f) => ({
  selector: `edge.fam-${f}`,
  style: {
    'line-color': FAMILY_STYLE[f].color,
    'target-arrow-color': FAMILY_STYLE[f].color,
    'line-style': FAMILY_STYLE[f].line,
    'target-arrow-shape': FAMILY_STYLE[f].arrow as never,
  },
}));
const gradeStyles: cytoscape.StylesheetJson = GRADES.map((g) => ({
  selector: `edge.grade-${g}`,
  style: { width: GRADE_STYLE[g].width, opacity: GRADE_STYLE[g].opacity },
}));

const STYLE: cytoscape.StylesheetJson = [
  {
    selector: 'node',
    style: {
      label: 'data(label)',
      'background-color': '#8aa0c8',
      color: '#e8eefc',
      'font-size': 11,
      'text-valign': 'bottom',
      'text-margin-y': 5,
      'text-outline-color': '#0b1020',
      'text-outline-width': 2,
      'text-max-width': '170px',
      'text-wrap': 'wrap',
      width: 20,
      height: 20,
      'border-width': 1,
      'border-color': '#0b1020',
    },
  },
  { selector: 'node.focal', style: { 'background-color': '#ffd166', width: 36, height: 36, 'font-size': 13, 'font-weight': 'bold' } },
  { selector: 'node.cause', style: { 'background-color': '#f08c5a', shape: 'round-rectangle' } },
  { selector: 'node.effect', style: { 'background-color': '#69b3e7', shape: 'round-rectangle' } },
  {
    selector: 'edge',
    style: {
      'curve-style': 'bezier',
      'arrow-scale': 0.9,
      label: 'data(label)',
      'font-size': 10,
      'font-weight': 'bold',
      color: '#ffffff',
      'text-background-color': '#0b1020',
      'text-background-opacity': 0.85,
      'text-background-padding': '2px',
      'text-background-shape': 'roundrectangle',
      'text-opacity': 1,
    },
  },
  ...familyStyles,
  ...gradeStyles,
  { selector: 'edge.selected', style: { 'underlay-color': '#ffffff', 'underlay-opacity': 0.35, 'underlay-padding': 4, opacity: 1 } },
  { selector: 'node.selected', style: { 'border-width': 3, 'border-color': '#ffffff' } },
];

/**
 * Graded causal graph: causes on the left, the event in the centre, effects on the right. Edge colour and
 * dash give the relation family, thickness and opacity the evidence grade (letter on the edge). Tapping an
 * edge opens its claim (source, tier, statement, mechanism, checks); tapping a node opens that event.
 */
export default function CausalGraph({
  model,
  selected,
  onSelectClaim,
  onOpenNode,
  label,
  height = 460,
}: {
  model: CausalGraphModel;
  selected: string | null;
  onSelectClaim: (id: string) => void;
  onOpenNode: (id: string) => void;
  label: string;
  height?: number;
}) {
  const el = useRef<HTMLDivElement>(null);
  const cyRef = useRef<cytoscape.Core | null>(null);
  const cb = useRef({ onSelectClaim, onOpenNode });
  cb.current = { onSelectClaim, onOpenNode };

  useEffect(() => {
    if (!el.current) return;
    let cy: cytoscape.Core;
    try {
      cy = cytoscape({ container: el.current, style: STYLE, minZoom: 0.2, maxZoom: 2.5, wheelSensitivity: 0.25, boxSelectionEnabled: false, autoungrabify: true });
    } catch {
      return;
    }
    cy.on('tap', 'edge', (evt) => cb.current.onSelectClaim(String(evt.target.id())));
    cy.on('tap', 'node', (evt) => {
      const n = evt.target;
      if (n.data('side') !== 'focal') cb.current.onOpenNode(String(n.id()));
    });
    cyRef.current = cy;
    return () => {
      cy.destroy();
      cyRef.current = null;
    };
  }, []);

  useEffect(() => {
    const cy = cyRef.current;
    if (!cy) return;
    cy.batch(() => {
      cy.elements().remove();
      cy.add(model.nodes.map((n) => ({ group: 'nodes' as const, data: { id: n.id, label: n.label, side: n.side }, position: { x: n.x, y: n.y }, classes: n.side })));
      cy.add(
        model.edges.map((e) => ({
          group: 'edges' as const,
          data: { id: e.id, source: e.source, target: e.target, label: e.grade },
          classes: `fam-${e.family} grade-${e.grade}`,
        })),
      );
    });
    cy.fit(undefined, 40);
    if (cy.zoom() > 1.3) {
      cy.zoom(1.3);
      cy.center();
    }
    if (!prefersReducedMotion()) {
      cy.edges().forEach((e, i) => {
        const o = Number(e.style('opacity'));
        e.style('opacity', 0);
        e.delay(60 * i).animate({ style: { opacity: o } }, { duration: 350 });
      });
    }
  }, [model]);

  useEffect(() => {
    const cy = cyRef.current;
    if (!cy) return;
    cy.elements().removeClass('selected');
    if (selected) {
      const e = cy.$id(selected);
      e.addClass('selected');
      e.connectedNodes().addClass('selected');
    }
  }, [selected, model]);

  return <div ref={el} className="v3-causal-canvas" style={{ height }} role="img" aria-label={label} data-testid="causal-canvas" />;
}
