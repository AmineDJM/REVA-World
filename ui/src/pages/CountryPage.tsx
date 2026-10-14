import { useMemo, useState } from 'react';
import { Link, useParams } from 'react-router';
import type { ColumnDef } from '@tanstack/react-table';
import { getBeliefs, getCoverage, getEntity, getGaps, getLocations, getOntology, getTimeseries } from '../api/client';
import { ApiError } from '../api/problem';
import {
  COUNTRY_CODES,
  type BeliefEstimate,
  type ConceptOut,
  type CountryCode,
  type TimeseriesPoint,
  type TypedValue,
  type Uncertainty,
} from '../api/types';
import { ContextBanner } from '../components/ContextBanner';
import { DataTable } from '../components/DataTable';
import { DefinitionPopover } from '../components/DefinitionPopover';
import { EnvelopeDetails } from '../components/EnvelopeDetails';
import { MapView } from '../components/MapView';
import { ProvenanceButton } from '../components/ProvenanceDrawer';
import { Empty, ErrorState, Loading } from '../components/StateViews';
import { TimeSeriesChart, type SeriesPoint } from '../components/TimeSeriesChart';
import { UncertaintyPanel } from '../components/UncertaintyPanel';
import { ValueView } from '../components/ValueView';
import { computeAvailability, yearOf } from '../lib/availability';
import { formatDateTime, humanize, unitLabel } from '../lib/format';
import { useApi } from '../lib/useApi';
import { PROFILE_PANELS } from '../lib/concepts';


function numeric(v: TypedValue | null | undefined): number | null {
  if (!v) return null;
  if (v.kind === 'decimal') {
    const n = Number(v.value);
    return Number.isFinite(n) ? n : null;
  }
  if (v.kind === 'float') return Number.isFinite(v.value) ? v.value : null;
  return null;
}

interface Row {
  key: string;
  period: string;
  year: number | null;
  value: TypedValue;
  uncertainty: Uncertainty;
  source: string;
  detail: string;
  provKind: 'estimate' | 'observation';
  provId: string;
}

const rowColumns: ColumnDef<Row, unknown>[] = [
  { header: 'Period', accessorKey: 'period' },
  { header: 'Value', id: 'value', cell: (c) => <ValueView value={c.row.original.value} showBasis /> },
  { header: 'Uncertainty', id: 'unc', cell: (c) => <UncertaintyPanel uncertainty={c.row.original.uncertainty} compact /> },
  { header: 'Source', accessorKey: 'source' },
  { header: 'Notes', accessorKey: 'detail' },
  {
    header: 'Provenance',
    id: 'prov',
    cell: (c) => <ProvenanceButton kind={c.row.original.provKind} id={c.row.original.provId} label="Trace" />,
  },
];

function beliefRows(data: BeliefEstimate[]): Row[] {
  return data.map((e) => {
    const flags = [...e.discrepancy_flags];
    if (e.alternatives.length) flags.push(`${e.alternatives.length} alternative(s) retained`);
    return {
      key: e.id,
      period: e.support.period_label ?? e.support.temporal?.label ?? String(e.support.temporal?.start ?? '—'),
      year: yearOf(e.support.temporal?.start ?? e.support.period_label),
      value: e.value,
      uncertainty: e.uncertainty,
      source: `${e.source_key ?? '—'}${e.observation_revision_no ? ` r${e.observation_revision_no}` : ''}`,
      detail: [humanize(e.modality), ...flags.map(humanize)].join('; '),
      provKind: 'estimate',
      provId: e.id,
    };
  });
}

function evidenceRows(points: TimeseriesPoint[]): Row[] {
  return points.map((p) => ({
    key: p.observation_id,
    period: p.period_label ?? p.valid_start ?? '—',
    year: yearOf(p.valid_start ?? p.period_label),
    value: p.value,
    uncertainty: p.uncertainty,
    source: `${p.source_key} r${p.revision_no}`,
    detail: `${humanize(p.temporal_kind)}; ${humanize(p.aggregation)}; retrieved ${formatDateTime(p.retrieved_at)}`,
    provKind: 'observation',
    provId: p.observation_id,
  }));
}

function ConceptPanel({ iso3, concept, title, definition }: { iso3: string; concept: string; title: string; definition?: ConceptOut }) {
  const [mode, setMode] = useState<'belief' | 'evidence'>('belief');
  const beliefs = useApi((s) => getBeliefs({ subject_id: iso3, concept, limit: 1000 }, s), [iso3, concept], mode === 'belief');
  const series = useApi((s) => getTimeseries({ subject_id: iso3, concept }, s), [iso3, concept], mode === 'evidence');
  const state = mode === 'belief' ? beliefs : series;
  const rows = useMemo(() => {
    if (mode === 'belief') return beliefs.data ? beliefRows(beliefs.data.data) : [];
    return series.data ? evidenceRows(series.data.data.points) : [];
  }, [mode, beliefs.data, series.data]);
  const sorted = useMemo(() => [...rows].sort((a, b) => (a.year ?? 0) - (b.year ?? 0)), [rows]);
  const chartPoints: SeriesPoint[] = useMemo(
    () => sorted.filter((r) => r.year !== null).map((r) => ({ x: r.year!, y: numeric(r.value) })),
    [sorted],
  );
  const latest = [...sorted].reverse().find((r) => r.value.kind !== 'missing') ?? sorted[sorted.length - 1];
  const missingPeriods = sorted.filter((r) => r.value.kind === 'missing').map((r) => r.period);
  const unit = definition?.semantic_profile?.unit_ref;
  const noRelease = beliefs.status === 'error' && beliefs.error instanceof ApiError && beliefs.error.code === 'no_release';
  const headingId = `panel-${concept.replace(/\W/g, '-')}`;

  return (
    <section className="panel" aria-labelledby={headingId}>
      <h2 id={headingId}>
        {title} <DefinitionPopover concept={definition} />
      </h2>
      <div className="tabs" role="group" aria-label={`${title} data layer`}>
        <button type="button" aria-pressed={mode === 'belief'} onClick={() => setMode('belief')}>
          Pinned release
        </button>
        <button type="button" aria-pressed={mode === 'evidence'} onClick={() => setMode('evidence')}>
          Source records
        </button>
      </div>
      {state.data && <ContextBanner meta={state.data} />}
      {state.status === 'loading' && <Loading what={title} />}
      {state.status === 'error' &&
        (noRelease ? (
          <Empty what="belief release">
            <p>No belief release has been published. Switch to source records to see evidence.</p>
          </Empty>
        ) : (
          <ErrorState error={state.error} what={title} onRetry={state.reload} />
        ))}
      {state.status === 'ok' && rows.length === 0 && (
        <Empty what={`${title.toLowerCase()} values`}>
          <p>REVA holds no {mode === 'belief' ? 'estimates' : 'source records'} for this country and concept. This is not a zero.</p>
        </Empty>
      )}
      {rows.length > 0 && (
        <>
          {latest && (
            <p className="headline">
              Latest ({latest.period}): <ValueView value={latest.value} compact showBasis />
            </p>
          )}
          {missingPeriods.length > 0 && (
            <p className="muted">Missing (shown as gaps, not zero): {missingPeriods.join(', ')}</p>
          )}
          <TimeSeriesChart points={chartPoints} label={`${title} by year for ${iso3}; table follows`} yLabel={unitLabel(unit) || 'value'} />
          <DataTable caption={`${title} — ${iso3} (${mode === 'belief' ? 'selected estimates' : 'latest source revisions'})`} columns={rowColumns} rows={sorted} rowKey={(r) => r.key} />
          <EnvelopeDetails meta={state.data} />
        </>
      )}
    </section>
  );
}

export default function CountryPage() {
  const { iso3: raw = '' } = useParams();
  const iso3 = raw.toUpperCase();
  const known = (COUNTRY_CODES as readonly string[]).includes(iso3);

  const entity = useApi((s) => getEntity(iso3, s), [iso3], known);
  const ontology = useApi((s) => getOntology(undefined, s), []);
  const locations = useApi((s) => getLocations({ entity_id: iso3 }, s), [iso3], known);
  const coverage = useApi((s) => getCoverage(undefined, s), [], known);
  const gaps = useApi((s) => getGaps({ entity_key: iso3 }, s), [iso3], known);

  const concepts = useMemo(() => {
    const m = new Map<string, ConceptOut>();
    for (const c of ontology.data?.data.concepts ?? []) m.set(c.concept_id, c);
    return m;
  }, [ontology.data]);

  const availability = useMemo(
    () => (entity.data ? computeAvailability(coverage.data?.data, gaps.data?.data, { [iso3]: entity.data.data.id }) : {}),
    [coverage.data, gaps.data, entity.data, iso3],
  );

  if (!known) {
    return (
      <>
        <h1>Country profile</h1>
        <ContextBanner meta={null} scope="Country profile" />
        <p role="alert">
          <strong>{raw || '(none)'}</strong> is not one of the V1 country profiles. Choose:{' '}
          {COUNTRY_CODES.map((c, i) => (
            <span key={c}>
              {i > 0 && ', '}
              <Link to={`/country/${c}`}>{c}</Link>
            </span>
          ))}
          .
        </p>
      </>
    );
  }

  const ent = entity.data?.data;
  return (
    <>
      <nav aria-label="Country profiles" className="inline-checks">
        {COUNTRY_CODES.map((c: CountryCode) => (
          <Link key={c} to={`/country/${c}`} aria-current={c === iso3 ? 'page' : undefined}>
            {c}
          </Link>
        ))}
      </nav>
      <h1>{ent ? `${ent.label} (${iso3})` : `Country ${iso3}`}</h1>
      <ContextBanner meta={entity.data} scope="Country profile" />
      {entity.status === 'loading' && <Loading what="entity" />}
      {entity.status === 'error' && <ErrorState error={entity.error} what="The entity" onRetry={entity.reload} />}

      <div className="grid">
        <section className="panel" aria-labelledby="support-h">
          <h2 id="support-h">Map support</h2>
          {locations.status === 'loading' && <Loading what="display geometry" />}
          {locations.status === 'error' && <ErrorState error={locations.error} what="Display geometry" onRetry={locations.reload} />}
          {locations.data && locations.data.data.features.length === 0 && <Empty what="display geometry for this entity" />}
          {locations.data && locations.data.data.features.length > 0 && ent && (
            <MapView
              collection={locations.data.data}
              availability={availability}
              focusEntityId={ent.id}
              label={`Display geometry of ${ent.label}`}
              height={280}
            />
          )}
          {locations.data && (
            <ul className="muted">
              {locations.data.data.features.map((f) => (
                <li key={f.id}>
                  {f.properties.name ?? f.properties.feature_key}: {humanize(f.properties.match_status)}
                </li>
              ))}
            </ul>
          )}
        </section>

        <section className="panel" aria-labelledby="identity-h">
          <h2 id="identity-h">Identity and timeline</h2>
          {ent && (
            <>
              <h3>Provider codes</h3>
              <ul>
                {ent.aliases.map((a) => (
                  <li key={`${a.provider}:${a.external_key}`}>
                    <span className="mono">{a.provider}</span>: {a.external_key}{' '}
                    <span className="muted">({humanize(a.confidence_kind)})</span>
                  </li>
                ))}
              </ul>
              <DataTable
                caption="Data held for this entity"
                columns={[
                  { header: 'Component', accessorFn: (c) => humanize(c.component) },
                  { header: 'Concept / layer', accessorFn: (c) => c.concept_id ?? c.layer ?? '—' },
                  { header: 'Records', accessorKey: 'count' },
                  { header: 'First period', accessorFn: (c) => c.first_period ?? '—' },
                  { header: 'Last period', accessorFn: (c) => c.last_period ?? '—' },
                ]}
                rows={ent.components}
                rowKey={(c, i) => `${c.component}-${c.concept_id ?? c.layer ?? i}`}
              />
              <h3>Supported contexts</h3>
              <ul>
                {ent.supported_contexts.map((c, i) => (
                  <li key={i}>
                    <span className="tag">{c.mode}</span> {c.description}
                    {c.knowledge_cutoff && <span className="muted"> · cutoff {formatDateTime(c.knowledge_cutoff)}</span>}
                    {c.origin === 'test_fixture' && <strong> · TEST FIXTURE DATA</strong>}
                  </li>
                ))}
              </ul>
            </>
          )}
        </section>
      </div>

      {gaps.data && gaps.data.data.length > 0 && (
        <section className="panel" aria-labelledby="cgaps-h">
          <h2 id="cgaps-h">Data gaps for {iso3}</h2>
          <DataTable
            caption="Open catalogue gaps (missing values are never imputed as zero)"
            columns={[
              { header: 'Concept', accessorKey: 'concept' },
              { header: 'Period', accessorKey: 'period' },
              { header: 'Gap', accessorFn: (g) => humanize(g.gap_kind) },
              { header: 'Reason', accessorKey: 'reason' },
              { header: 'Action', accessorKey: 'action' },
            ]}
            rows={gaps.data.data}
            rowKey={(g, i) => `${g.requirement_id}-${g.period}-${i}`}
          />
        </section>
      )}

      {PROFILE_PANELS.map((p) => (
        <ConceptPanel key={`${iso3}-${p.concept}`} iso3={iso3} concept={p.concept} title={p.title} definition={concepts.get(p.concept)} />
      ))}
    </>
  );
}
