import { useMemo, useState, type FormEvent } from 'react';
import type { ColumnDef } from '@tanstack/react-table';
import { getRelations } from '../api/client';
import { COUNTRY_CODES, type Relation } from '../api/types';
import { ContextBanner } from '../components/ContextBanner';
import { DataTable } from '../components/DataTable';
import { EnvelopeDetails } from '../components/EnvelopeDetails';
import { GraphView, roleLabel } from '../components/GraphView';
import { ProvenanceButton } from '../components/ProvenanceDrawer';
import { Empty, ErrorState, Loading } from '../components/StateViews';
import { ValueView } from '../components/ValueView';
import { formatDateTime, humanize } from '../lib/format';
import { useApi } from '../lib/useApi';

interface Filters {
  entity: string;
  layer: string;
  period: string;
  asOf: string;
  origin: string;
  depth: number;
  maxEdges: number;
}

const DEFAULTS: Filters = { entity: 'FRA', layer: '', period: '2023', asOf: '', origin: '', depth: 1, maxEdges: 100 };

/** datetime-local value (interpreted as UTC) -> ISO-8601 with Z. */
export function localUtcToIso(v: string): string | undefined {
  if (!v) return undefined;
  const d = new Date(/T\d{2}:\d{2}$/.test(v) ? `${v}:00Z` : `${v}Z`);
  return Number.isNaN(d.getTime()) ? undefined : d.toISOString();
}

const relColumns: ColumnDef<Relation, unknown>[] = [
  { header: 'Layer', accessorFn: (r) => humanize(r.layer) },
  { header: 'Predicate', accessorKey: 'predicate', cell: (c) => <span className="mono">{String(c.getValue())}</span> },
  {
    header: 'Participants (role: entity)',
    id: 'parts',
    cell: (c) => (
      <ul className="json-list">
        {c.row.original.participants.map((p) => (
          <li key={p.ordinal}>
            <strong>{roleLabel(p.role)}</strong>: {p.entity_label} <span className="muted">({humanize(p.direction)})</span>
          </li>
        ))}
      </ul>
    ),
  },
  { header: 'Period', accessorFn: (r) => r.support.period_label ?? '—' },
  { header: 'Value', id: 'value', cell: (c) => <ValueView value={c.row.original.value} showBasis /> },
  { header: 'Origin', accessorFn: (r) => (r.origin === 'test_fixture' ? 'TEST FIXTURE' : (r.origin ?? '—')) },
];

export default function GraphPage() {
  const [draft, setDraft] = useState<Filters>(DEFAULTS);
  const [filters, setFilters] = useState<Filters>(DEFAULTS);
  const [selected, setSelected] = useState<string | null>(null);

  const rel = useApi(
    (s) =>
      getRelations(
        {
          entity_id: filters.entity || undefined,
          layer: filters.layer || undefined,
          period: /^\d{4}$/.test(filters.period) ? Number(filters.period) : undefined,
          as_of: localUtcToIso(filters.asOf),
          origin: filters.origin || undefined,
          depth: filters.depth,
          max_edges: filters.maxEdges,
        },
        s,
      ),
    [JSON.stringify(filters)],
  );

  const graph = rel.data?.data;
  const layers = useMemo(() => [...new Set(graph?.relations.map((r) => r.layer) ?? [])].sort(), [graph]);
  const chosen = graph?.relations.find((r) => r.id === selected);

  const submit = (e: FormEvent) => {
    e.preventDefault();
    setSelected(null);
    setFilters(draft);
  };

  return (
    <>
      <h1>Graph</h1>
      <ContextBanner
        meta={rel.data}
        scope="Bounded relation graph"
        referenceTime={filters.period ? `calendar year ${filters.period}` : 'all periods'}
      />
      <form className="panel form-grid" onSubmit={submit} aria-label="Graph filters">
        <label>
          Focus entity
          <select value={draft.entity} onChange={(e) => setDraft({ ...draft, entity: e.target.value })}>
            <option value="">(none — all relations)</option>
            {COUNTRY_CODES.map((c) => (
              <option key={c}>{c}</option>
            ))}
          </select>
        </label>
        <label>
          Layer
          <input
            type="text"
            list="layer-options"
            value={draft.layer}
            onChange={(e) => setDraft({ ...draft, layer: e.target.value })}
            placeholder="all layers"
          />
          <datalist id="layer-options">
            {layers.map((l) => (
              <option key={l} value={l} />
            ))}
          </datalist>
        </label>
        <label>
          Period (year)
          <input type="text" inputMode="numeric" value={draft.period} onChange={(e) => setDraft({ ...draft, period: e.target.value })} />
        </label>
        <label>
          Recorded as of (UTC)
          <input type="datetime-local" value={draft.asOf} onChange={(e) => setDraft({ ...draft, asOf: e.target.value })} />
        </label>
        <label>
          Context origin
          <select value={draft.origin} onChange={(e) => setDraft({ ...draft, origin: e.target.value })}>
            <option value="">any</option>
            <option value="live">live</option>
            <option value="test_fixture">test fixture</option>
          </select>
        </label>
        <label>
          Depth
          <select value={draft.depth} onChange={(e) => setDraft({ ...draft, depth: Number(e.target.value) })}>
            <option value={1}>1</option>
            <option value={2}>2</option>
          </select>
        </label>
        <label>
          Max relations
          <input
            type="number"
            min={1}
            max={500}
            value={draft.maxEdges}
            onChange={(e) => setDraft({ ...draft, maxEdges: Math.max(1, Math.min(500, Number(e.target.value) || 1)) })}
          />
        </label>
        <div>
          <button type="submit" className="primary">
            Apply
          </button>
        </div>
      </form>

      {rel.status === 'loading' && <Loading what="relations" />}
      {rel.status === 'error' && <ErrorState error={rel.error} what="Relations" onRetry={rel.reload} />}
      {graph && (
        <section className="panel" aria-labelledby="graph-h">
          <h2 id="graph-h">Trade hyperedges</h2>
          <p className={graph.truncated ? 'bound-flag' : 'muted'} role="status">
            {graph.truncated
              ? `Bounded: showing ${graph.relations.length} relations; at least ${graph.matched_lower_bound} match (max ${graph.max_edges}, depth ${graph.depth}). Narrow the filters to see the rest.`
              : `Complete within bounds: ${graph.relations.length} relations (max ${graph.max_edges}, depth ${graph.depth}).`}
          </p>
          {graph.relations.length === 0 ? (
            <Empty what="relations for these filters" />
          ) : (
            <>
              <GraphView
                graph={graph}
                selectedRelationId={selected}
                onSelectRelation={setSelected}
                label={`Graph of ${graph.relations.length} relations and ${graph.nodes.length} entities; the table below lists the same hyperedges`}
              />
              <p className="muted">
                Diamonds are relations (hyperedges); edges are labelled with participant roles (reporter, partner, product…).
                Dashed diamonds carry a missing value.
              </p>
              <DataTable
                caption="Hyperedges and participant roles (select a row to inspect)"
                columns={relColumns}
                rows={graph.relations}
                rowKey={(r) => r.id}
                onRowClick={(r) => setSelected(r.id)}
                selectedKey={selected}
              />
            </>
          )}
          {chosen && (
            <article className="panel" aria-label="Selected relation">
              <h3>
                {humanize(chosen.layer)} · {chosen.support.period_label ?? '—'}
              </h3>
              <dl className="kv">
                {chosen.participants.map((p) => (
                  <div className="kv-row" key={p.ordinal}>
                    <dt>{roleLabel(p.role)}</dt>
                    <dd>
                      {p.entity_label} <span className="muted mono">{p.role}</span>
                    </dd>
                  </div>
                ))}
                <div className="kv-row">
                  <dt>Value</dt>
                  <dd>
                    <ValueView value={chosen.value} showBasis />
                  </dd>
                </div>
                <div className="kv-row">
                  <dt>Concept</dt>
                  <dd className="mono">{chosen.concept_id ?? '—'}</dd>
                </div>
                <div className="kv-row">
                  <dt>Recorded</dt>
                  <dd>{formatDateTime(chosen.recorded_at)}</dd>
                </div>
              </dl>
              {chosen.evidence_observation_id && (
                <ProvenanceButton kind="observation" id={chosen.evidence_observation_id} label="Trace source record" />
              )}
            </article>
          )}
          <EnvelopeDetails meta={rel.data} />
        </section>
      )}
    </>
  );
}
