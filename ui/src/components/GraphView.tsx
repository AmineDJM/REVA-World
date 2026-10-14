import { useEffect, useRef } from 'react';
import cytoscape from 'cytoscape';
import type { RelationGraph } from '../api/types';
import { prefersReducedMotion } from '../lib/useApi';
import { humanize, valueToText } from '../lib/format';

export interface GraphViewProps {
  graph: RelationGraph;
  selectedRelationId?: string | null;
  onSelectRelation?: (id: string) => void;
  label: string;
  height?: number;
}

/** Strip the concept namespace from a role, e.g. "role.trade.reporter" -> "reporter". */
export function roleLabel(role: string): string {
  const parts = role.split('.');
  return parts[parts.length - 1] ?? role;
}

/**
 * Bounded Cytoscape view. Each relation is a hyperedge node (diamond) linked
 * to its participants by role-labelled edges, so n-ary relations stay n-ary.
 */
export function GraphView({ graph, selectedRelationId, onSelectRelation, label, height = 460 }: GraphViewProps) {
  const el = useRef<HTMLDivElement>(null);
  const cyRef = useRef<cytoscape.Core | null>(null);
  const onSelect = useRef(onSelectRelation);
  onSelect.current = onSelectRelation;

  useEffect(() => {
    if (!el.current) return;
    const elements: cytoscape.ElementDefinition[] = [];
    for (const n of graph.nodes) {
      elements.push({ data: { id: `e:${n.id}`, label: n.label, kind: n.kind }, classes: 'entity' });
    }
    for (const r of graph.relations) {
      const hid = `r:${r.id}`;
      const period = r.support.period_label ?? r.support.temporal?.label ?? '';
      elements.push({
        data: { id: hid, rel: r.id, label: `${humanize(r.layer)}${period ? ` ${period}` : ''}`, value: valueToText(r.value) },
        classes: `hyperedge${r.value?.kind === 'missing' ? ' missing' : ''}`,
      });
      for (const p of r.participants) {
        const nid = `e:${p.entity_id}`;
        if (!elements.some((x) => x.data.id === nid)) {
          elements.push({ data: { id: nid, label: p.entity_label, kind: p.entity_kind }, classes: 'entity' });
        }
        elements.push({
          data: { id: `${hid}-${p.ordinal}`, source: hid, target: nid, label: roleLabel(p.role) },
          classes: 'role',
        });
      }
    }
    let cy: cytoscape.Core;
    try {
      cy = cytoscape({
        container: el.current,
        elements,
        wheelSensitivity: 0.3,
        motionBlur: false,
        textureOnViewport: prefersReducedMotion(),
        style: [
          {
            selector: 'node.entity',
            style: {
              'background-color': '#dbe4ee',
              'border-color': '#1f4e79',
              'border-width': 1,
              label: 'data(label)',
              'font-size': 10,
              'text-valign': 'bottom',
              'text-margin-y': 4,
              width: 26,
              height: 26,
            },
          },
          {
            selector: 'node.hyperedge',
            style: {
              shape: 'diamond',
              'background-color': '#f5f5f4',
              'border-color': '#57534e',
              'border-width': 1,
              label: 'data(label)',
              'font-size': 8,
              width: 16,
              height: 16,
            },
          },
          { selector: 'node.hyperedge.missing', style: { 'border-style': 'dashed' } },
          { selector: 'node:selected', style: { 'border-width': 3, 'border-color': '#111827' } },
          {
            selector: 'edge.role',
            style: {
              width: 1,
              'line-color': '#9ca3af',
              label: 'data(label)',
              'font-size': 8,
              'text-background-color': '#ffffff',
              'text-background-opacity': 1,
              'curve-style': 'bezier',
            },
          },
        ],
        layout: { name: 'cose', animate: false, randomize: false, nodeRepulsion: () => 8000 } as cytoscape.LayoutOptions,
        autoungrabify: false,
        boxSelectionEnabled: false,
      });
    } catch {
      return;
    }
    cy.on('tap', 'node.hyperedge', (evt) => onSelect.current?.(String(evt.target.data('rel'))));
    cyRef.current = cy;
    cy.fit(undefined, 20);
    return () => {
      cy.destroy();
      cyRef.current = null;
    };
  }, [graph]);

  useEffect(() => {
    const cy = cyRef.current;
    if (!cy) return;
    cy.$(':selected').unselect();
    if (selectedRelationId) cy.$id(`r:${selectedRelationId}`).select();
  }, [selectedRelationId, graph]);

  return <div ref={el} className="graph" style={{ height }} role="img" aria-label={label} />;
}
