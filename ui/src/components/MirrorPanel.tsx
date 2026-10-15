import { useMemo } from 'react';
import { getRelations } from '../api/client';
import { ProvenanceButton } from './ProvenanceDrawer';
import { ErrorState, Loading } from './StateViews';
import { MissingValue } from './MissingValue';
import { displayDecimal, missingReasonLabel } from '../lib/format';
import { mirrorFlows, type FlowRow } from '../lib/worldmap';
import { useApi } from '../lib/useApi';

export const MIRROR_LABEL = 'reported by partner';

const FLOW_TEXT: Record<string, (country: string) => string> = {
  M: (c) => `partner's imports from ${c}`,
  X: (c) => `partner's exports to ${c}`,
};

function Value({ row }: { row: FlowRow }) {
  if (row.value === null) return <MissingValue reason={row.missingReason ?? 'unavailable'} />;
  const d = displayDecimal(row.value, 'unit.usd');
  return (
    <span title={`exact: ${row.value} US$`} data-exact={row.value}>
      <span className="num">{d.text}</span> <span className="unit">US$</span>
      {row.valuation && <span className="basis"> · {row.valuation}</span>}
    </span>
  );
}

export interface MirrorPanelProps {
  /** Entity id or ISO3 of the country. */
  entity: string;
  /** Display name of the country. */
  countryLabel: string;
  /** Entity UUID of the country (to recognise it as partner); resolved by the caller. */
  entityId: string | null;
  /** Evidence as known at this instant (e.g. a scenario's cutoff); omitted = now. */
  asOf?: string | null;
  headingLevel?: 2 | 3;
}

/**
 * Mirror statistics: what other reporters say about their trade with a country. Shown in its
 * own panel and table, labelled "reported by partner" on every row, and never used to fill
 * the country's own cells, map polygon or model inputs.
 */
export function MirrorPanel({ entity, countryLabel, entityId, asOf, headingLevel = 2 }: MirrorPanelProps) {
  const rel = useApi((s) => getRelations({ entity_id: entity, layer: 'trade.crude_petroleum', as_of: asOf ?? undefined, max_edges: 500 }, s), [entity, asOf]);
  const rows = useMemo(() => (rel.data && entityId ? mirrorFlows(rel.data.data.relations, entityId) : []), [rel.data, entityId]);
  const H = headingLevel === 2 ? 'h2' : 'h3';
  const hid = `mirror-h-${entity}`;
  return (
    <section className="panel mirror-panel" aria-labelledby={hid} data-testid="mirror-panel">
      <H id={hid}>
        Mirror view: {countryLabel} as {MIRROR_LABEL}s see it
      </H>
      <p className="mirror-note">
        <span className="mirror-badge">{MIRROR_LABEL}</span> These are the partners' own customs figures for their crude
        (HS 2709) trade with {countryLabel}. They are a separate lineage: valuation (CIF imports, FOB exports), timing and
        coverage differ, so they are <strong>never</strong> used to fill {countryLabel}'s own values, map polygon or scenario
        inputs, and they are not summed into a total.
      </p>
      {rel.status === 'loading' && <Loading what="partner-reported flows" />}
      {rel.status === 'error' && <ErrorState error={rel.error} what="Partner-reported flows" onRetry={rel.reload} />}
      {rel.data && rows.length === 0 && (
        <p className="muted" role="status">
          No partner reports a crude flow with {countryLabel} in the evidence held. This is an absence of records, not a zero
          flow.
        </p>
      )}
      {rows.length > 0 && (
        <div className="table-wrap">
          <table className="data-table" data-testid="mirror-table">
            <caption>
              Crude (HS 2709) flows {MIRROR_LABEL} — {rows.length} row{rows.length === 1 ? '' : 's'}; partner figures, not{' '}
              {countryLabel}'s own
            </caption>
            <thead>
              <tr>
                <th scope="col">Reporting partner</th>
                <th scope="col">Year</th>
                <th scope="col">Partner's flow</th>
                <th scope="col">Value ({MIRROR_LABEL})</th>
                <th scope="col">Source</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((r) => (
                <tr key={r.relationId} data-mirror-row="">
                  <th scope="row">{r.reporterLabel}</th>
                  <td>{r.period ?? '—'}</td>
                  <td>{(FLOW_TEXT[r.flow] ?? ((c: string) => `flow ${r.flow} with ${c}`))(countryLabel)}</td>
                  <td>
                    <Value row={r} />
                    <span className="mirror-badge mirror-badge-inline">{MIRROR_LABEL}</span>
                  </td>
                  <td>
                    {r.observationId ? <ProvenanceButton kind="observation" id={r.observationId} label="Trace" /> : <span className="muted">no observation id</span>}
                    {r.missingReason && <span className="field-hint"> {missingReasonLabel(r.missingReason)}</span>}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </section>
  );
}
