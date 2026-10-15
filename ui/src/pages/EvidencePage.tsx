import { useMemo, useState } from 'react';
import type { ColumnDef } from '@tanstack/react-table';
import { getEvidence, getEvidenceItem, getObservations } from '../api/client';
import { COUNTRY_CODES, type EvidenceItem, type Observation } from '../api/types';
import { ContextBanner } from '../components/ContextBanner';
import { DataTable } from '../components/DataTable';
import { EnvelopeDetails } from '../components/EnvelopeDetails';
import { JsonView } from '../components/JsonView';
import { MissingValue } from '../components/MissingValue';
import { ProvenanceButton } from '../components/ProvenanceDrawer';
import { Empty, ErrorState, Loading } from '../components/StateViews';
import { UncertaintyPanel } from '../components/UncertaintyPanel';
import { UncertaintyLegend, glyphKindsOf } from '../components/UncertaintyGlyph';
import { ValueView } from '../components/ValueView';
import { formatDateTime, humanize, isRecord, localUtcToIso } from '../lib/format';
import { useApi } from '../lib/useApi';
import { PROFILE_PANELS } from '../lib/concepts';

/** Card fields in the order of reva.evidence.cards.EvidenceCard. */
export const CARD_FIELDS: string[] = [
  'question',
  'estimand',
  'treatment',
  'comparator',
  'outcome',
  'population',
  'geography',
  'period',
  'horizon',
  'units',
  'effect_scale',
  'study_ids',
  'dataset_families',
  'sample_size',
  'design',
  'coefficient',
  'standard_error',
  'covariance_ref',
  'identification_assumptions',
  'confounders',
  'selection',
  'interference',
  'measurement_validity',
  'risk_of_bias',
  'robustness',
  'replication_status',
  'publication_status',
  'retraction_status',
  'external_validity',
  'mechanism',
  'competing_explanations',
  'extraction_method',
  'extraction_version',
  'human_review',
  'licence',
  'evidence_kind',
  'ratings',
  'relevance_to_reva',
];

/** A card field is "not extracted" when absent, null or the explicit marker. */
export function isNotExtracted(v: unknown): boolean {
  return v === undefined || v === null || (isRecord(v) && (v.missing === 'not_extracted' || v.kind === 'missing'));
}

function CardField({ value }: { value: unknown }) {
  if (isNotExtracted(value)) {
    const reason = isRecord(value) && typeof value.reason === 'string' ? value.reason : 'not_extracted';
    return <MissingValue reason={reason} />;
  }
  return <JsonView value={value} />;
}

function CardDetail({ id }: { id: string }) {
  const item = useApi((s) => getEvidenceItem(id, s), [id]);
  if (item.status === 'loading') return <Loading what="evidence item" />;
  if (item.status === 'error') return <ErrorState error={item.error} what="The evidence item" onRetry={item.reload} />;
  if (!item.data) return null;
  const d = item.data.data;
  const card = d.card ?? {};
  const spans = Array.isArray(card.source_spans) ? (card.source_spans as Record<string, unknown>[]) : [];
  return (
    <article aria-label={`Evidence: ${d.title}`}>
      <ContextBanner meta={item.data} scope="Evidence item" />
      <h3>{d.title}</h3>
      <p>
        <span className="tag">{humanize(d.kind)}</span> {d.status && <span className="tag">status: {d.status}</span>}{' '}
        {d.evidence_kind && <span className="tag">{humanize(d.evidence_kind)}</span>}{' '}
        {d.claim_key && (
          <span className="mono">
            {d.claim_key} v{d.claim_version}
          </span>
        )}
      </p>
      {d.kind === 'scientific_card' && (
        <>
          <dl className="kv">
            {CARD_FIELDS.map((f) => (
              <div className="kv-row" key={f}>
                <dt>{humanize(f)}</dt>
                <dd>
                  <CardField value={card[f]} />
                </dd>
              </div>
            ))}
          </dl>
          <h4>Source spans</h4>
          {spans.length === 0 ? (
            <MissingValue reason="not_extracted" />
          ) : (
            <ul>
              {spans.map((s, i) => (
                <li key={i}>
                  <span className="tag">{String(s.artifact)}</span> <span className="mono">{String(s.locator)}</span>
                  <blockquote>{String(s.quote)}</blockquote>
                </li>
              ))}
            </ul>
          )}
          {(d.effects ?? []).length > 0 && (
            <>
              <h4>Extracted effects</h4>
              <JsonView value={d.effects} />
            </>
          )}
        </>
      )}
      {d.kind === 'study' && d.study && <JsonView value={d.study} />}
      {d.kind === 'source_disagreement' && d.disagreement && (
        <>
          <p>{d.disagreement.note}</p>
          <p>
            {d.disagreement.subject.label} · <span className="mono">{d.disagreement.concept_id}</span> ·{' '}
            {d.disagreement.support.period_label ?? '—'}
          </p>
          <h4>Selected</h4>
          <JsonView value={d.disagreement.chosen} />
          <h4>Retained alternatives</h4>
          <JsonView value={d.disagreement.alternatives} />
          <p>Flags: {d.disagreement.discrepancy_flags.map(humanize).join(', ') || 'none'}</p>
          <ProvenanceButton kind="estimate" id={d.disagreement.estimate_id} label="Trace selected estimate" />
        </>
      )}
      <EnvelopeDetails meta={item.data} />
    </article>
  );
}

const evidenceColumns: ColumnDef<EvidenceItem, unknown>[] = [
  { header: 'Title', accessorKey: 'title' },
  { header: 'Kind', accessorFn: (e) => humanize(e.kind) },
  { header: 'Summary', accessorFn: (e) => e.summary ?? '—' },
  { header: 'Status', accessorFn: (e) => e.status ?? '—' },
  { header: 'Recorded', accessorFn: (e) => formatDateTime(e.recorded_at) },
];

const obsColumns: ColumnDef<Observation, unknown>[] = [
  { header: 'Logical key', accessorKey: 'logical_key', cell: (c) => <span className="mono">{String(c.getValue())}</span> },
  { header: 'Source', accessorKey: 'source_key' },
  { header: 'Period', accessorFn: (o) => o.support.period_label ?? '—' },
  { header: 'Revision', accessorKey: 'revision_no' },
  { header: 'Value', id: 'value', cell: (c) => <ValueView value={c.row.original.value} showBasis /> },
  { header: 'Uncertainty', id: 'unc', cell: (c) => <UncertaintyPanel uncertainty={c.row.original.uncertainty} compact /> },
  { header: 'Retrieved', accessorFn: (o) => formatDateTime(o.retrieved_at) },
  {
    header: 'State',
    id: 'state',
    accessorFn: (o) =>
      [o.superseded_by_id ? 'superseded' : 'latest', o.retracted ? 'retracted' : '', o.origin === 'test_fixture' ? 'fixture' : '']
        .filter(Boolean)
        .join(', '),
  },
];

function ObservationDrilldown({ obs, lineage }: { obs: Observation; lineage: Observation[] }) {
  const sel = obs.raw_selector ?? {};
  return (
    <article className="panel" aria-label="Raw-field drilldown">
      <h3>Raw-field drilldown</h3>
      <dl className="kv">
        <div className="kv-row">
          <dt>Value</dt>
          <dd>
            <ValueView value={obs.value} showBasis />
          </dd>
        </div>
        <div className="kv-row">
          <dt>Uncertainty</dt>
          <dd>
            <UncertaintyPanel uncertainty={obs.uncertainty} compact />
          </dd>
        </div>
        <div className="kv-row">
          <dt>Raw artifact</dt>
          <dd className="mono break">{String(sel.artifact_sha256 ?? 'not recorded')}</dd>
        </div>
        <div className="kv-row">
          <dt>Raw selector</dt>
          <dd>
            <JsonView value={sel} />
          </dd>
        </div>
        <div className="kv-row">
          <dt>Source flags</dt>
          <dd>
            <JsonView value={obs.source_flags} emptyLabel="none" />
          </dd>
        </div>
        <div className="kv-row">
          <dt>Connector</dt>
          <dd>
            {obs.connector}@{obs.connector_version} · parser {obs.parser_version}
          </dd>
        </div>
        <div className="kv-row">
          <dt>Source published</dt>
          <dd>{obs.source_published_at ? formatDateTime(obs.source_published_at) : 'not reported by source'}</dd>
        </div>
        <div className="kv-row">
          <dt>Availability evidence</dt>
          <dd>
            {obs.source_available_at ? formatDateTime(obs.source_available_at) : 'unknown'}{' '}
            {obs.availability_basis && <JsonView value={obs.availability_basis} />}
          </dd>
        </div>
        <div className="kv-row">
          <dt>Retrieved / recorded</dt>
          <dd>
            {formatDateTime(obs.retrieved_at)} / {formatDateTime(obs.recorded_at)}
          </dd>
        </div>
      </dl>
      <ProvenanceButton kind="observation" id={obs.id} label="Open full lineage to raw bytes" />

      <h4>Revision timeline for {obs.logical_key}</h4>
      <ol className="revision-timeline">
        {lineage.map((r) => (
          <li key={r.id} aria-current={r.id === obs.id ? 'true' : undefined}>
            <strong>r{r.revision_no}</strong> recorded {formatDateTime(r.recorded_at)}
            {r.source_revision_id && <span className="mono"> · provider rev {r.source_revision_id}</span>}
            {' · '}
            <ValueView value={r.value} />
            {r.superseded_by_id && <span className="muted"> · superseded</span>}
            {r.retracted && <strong> · retracted</strong>}
          </li>
        ))}
      </ol>
      <p className="muted">
        Corrections append new revisions. An as-of query before a correction still returns the earlier revision.
      </p>
    </article>
  );
}

export default function EvidencePage() {
  const evidence = useApi((s) => getEvidence({ limit: 200 }, s), []);
  const [selected, setSelected] = useState<string | null>(null);
  const [country, setCountry] = useState<string>('FRA');
  const [concept, setConcept] = useState<string>(PROFILE_PANELS[2]!.concept);
  const [year, setYear] = useState<string>('2023');
  const [obsId, setObsId] = useState<string | null>(null);
  const [asOf, setAsOf] = useState('');
  const asOfIso = localUtcToIso(asOf);

  const y = /^\d{4}$/.test(year) ? Number(year) : undefined;
  const obs = useApi(
    (s) =>
      getObservations(
        { subject_id: country, concept, period_start: y, period_end: y, revisions: 'all', as_of: asOfIso, limit: 1000 },
        s,
      ),
    [country, concept, y, asOfIso],
  );

  const bySource = useMemo(() => {
    const m = new Map<string, Observation[]>();
    for (const o of obs.data?.data ?? []) {
      if (o.superseded_by_id) continue;
      const k = o.support.period_label ?? '—';
      m.set(k, [...(m.get(k) ?? []), o]);
    }
    return m;
  }, [obs.data]);

  const chosen = obs.data?.data.find((o) => o.id === obsId);
  const lineage = useMemo(
    () => (chosen ? (obs.data?.data ?? []).filter((o) => o.logical_key === chosen.logical_key && o.source_key === chosen.source_key).sort((a, b) => a.revision_no - b.revision_no) : []),
    [chosen, obs.data],
  );

  return (
    <>
      <h1>Evidence</h1>
      <ContextBanner meta={obs.data ?? evidence.data} scope="Source records and scientific evidence" />

      <section className="panel" aria-labelledby="records-h">
        <h2 id="records-h">Source records</h2>
        <form className="form-grid" onSubmit={(e) => e.preventDefault()} aria-label="Filter source records">
          <label>
            Country
            <select value={country} onChange={(e) => setCountry(e.target.value)}>
              {COUNTRY_CODES.map((c) => (
                <option key={c}>{c}</option>
              ))}
            </select>
          </label>
          <label>
            Concept
            <select value={concept} onChange={(e) => setConcept(e.target.value)}>
              {PROFILE_PANELS.map((p) => (
                <option key={p.concept} value={p.concept}>
                  {p.title}
                </option>
              ))}
            </select>
          </label>
          <label>
            Reference year
            <input type="text" inputMode="numeric" value={year} onChange={(e) => setYear(e.target.value)} aria-describedby="year-hint" />
            <span id="year-hint" className="field-hint">
              Leave empty for all years.
            </span>
          </label>
          <label>
            Recorded as of (UTC)
            <input type="datetime-local" value={asOf} onChange={(e) => setAsOf(e.target.value)} aria-describedby="asof-hint" />
            <span id="asof-hint" className="field-hint">
              Only records committed by this instant; later revisions are excluded.
            </span>
          </label>
        </form>
        {obs.status === 'loading' && <Loading what="source records" />}
        {obs.status === 'error' && <ErrorState error={obs.error} what="Source records" onRetry={obs.reload} />}
        {obs.data && obs.data.data.length === 0 && <Empty what="source records for this filter" />}
        {obs.data && obs.data.data.length > 0 && (
          <>
            <h3>Source comparison</h3>
            <UncertaintyLegend present={glyphKindsOf(obs.data.data)} />
            <DataTable
              caption="Latest revision per source, by period"
              columns={[
                { header: 'Period', accessorKey: 'period' },
                {
                  header: 'Sources',
                  id: 'srcs',
                  cell: (c) => (
                    <ul className="json-list">
                      {c.row.original.items.map((o) => (
                        <li key={o.id}>
                          {o.source_key}: <ValueView value={o.value} showBasis />{' '}
                          {o.origin === 'test_fixture' && <strong className="fixture-inline">fixture</strong>}
                        </li>
                      ))}
                    </ul>
                  ),
                },
                {
                  header: 'Agreement',
                  id: 'agree',
                  accessorFn: (r) => (r.items.length < 2 ? 'single source' : 'multiple sources — definitions may differ; not averaged'),
                },
              ]}
              rows={[...bySource.entries()].map(([period, items]) => ({ period, items }))}
              rowKey={(r) => r.period}
            />
            <h3>All revisions</h3>
            <DataTable
              caption="Observations (select a row for raw fields and revision timeline)"
              columns={obsColumns}
              rows={obs.data.data}
              rowKey={(o) => o.id}
              onRowClick={(o) => setObsId(o.id)}
              selectedKey={obsId}
            />
            {obs.data.next_cursor && <p className="bound-flag">More records exist; narrow the filter.</p>}
            {chosen && <ObservationDrilldown obs={chosen} lineage={lineage} />}
            <EnvelopeDetails meta={obs.data} />
          </>
        )}
      </section>

      <section className="panel" aria-labelledby="cards-h">
        <h2 id="cards-h">Scientific cards, studies and source disagreements</h2>
        {evidence.status === 'loading' && <Loading what="evidence" />}
        {evidence.status === 'error' && <ErrorState error={evidence.error} what="Evidence" onRetry={evidence.reload} />}
        {evidence.data && evidence.data.data.length === 0 && <Empty what="evidence items" />}
        {evidence.data && evidence.data.data.length > 0 && (
          <DataTable
            caption="Evidence items (select a row)"
            columns={evidenceColumns}
            rows={evidence.data.data}
            rowKey={(e) => e.id}
            onRowClick={(e) => setSelected(e.id)}
            selectedKey={selected}
          />
        )}
        {selected && <CardDetail id={selected} />}
      </section>
    </>
  );
}
