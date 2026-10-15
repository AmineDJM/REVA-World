import { useCallback, useEffect, useMemo, useState } from 'react';
import { Link, useParams } from 'react-router';
import type { ColumnDef } from '@tanstack/react-table';
import { getBeliefRelease, getBeliefs, getCoverage, getEntity, getGaps, getLocations, getOntology, getTimeseries } from '../api/client';
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
import { ContextBanner, combineOrigins } from '../components/ContextBanner';
import { DataTable } from '../components/DataTable';
import { DefinitionPopover } from '../components/DefinitionPopover';
import { EnvelopeDetails } from '../components/EnvelopeDetails';
import { MapView } from '../components/MapView';
import { MirrorPanel } from '../components/MirrorPanel';
import { ProvenanceButton } from '../components/ProvenanceDrawer';
import { Empty, ErrorState, Loading } from '../components/StateViews';
import { TimeSeriesChart, type SeriesPoint } from '../components/TimeSeriesChart';
import { UncertaintyPanel } from '../components/UncertaintyPanel';
import { UncertaintyLegend, glyphKindsOf } from '../components/UncertaintyGlyph';
import { ValueView } from '../components/ValueView';
import { computeAvailability, yearOf } from '../lib/availability';
import { formatDateTime, humanize, sourceFlagNotes, unitLabel } from '../lib/format';
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
  /** Observation the value comes from (joins provider source flags). */
  observationId: string | null;
  /** Provider source-flag notes (e.g. provider estimate, not directly reported). */
  flags: string[];
  /** source_flags was cut to its size bound (the full record is at /v1/observations). */
  flagsTruncated: boolean;
  /** Record origin (timeseries points carry it; belief rows inherit the release origin). */
  origin: string | null;
  /** Belief rows: publication of the concept is paused; the value is the last accepted one. */
  stale: boolean;
  staleReason: string | null;
  proposalId: string | null;
  /** Belief rows: release the row was read from, when it differs from the baseline release. */
  servedFromReleaseId: string | null;
}

/** Stale / served-from-earlier-release badges for one belief row. */
function StaleNotes({ row, releaseSeq }: { row: Row; releaseSeq: (id: string) => number | null | undefined }) {
  if (!row.stale && !row.servedFromReleaseId) return null;
  const seq = row.servedFromReleaseId ? releaseSeq(row.servedFromReleaseId) : null;
  return (
    <span className="stale-notes" data-testid="stale-notes">
      {row.stale && (
        <span className="badge badge-stale" title="Publication of this concept is paused; this is the last accepted value, not a fresh one">
          stale{row.staleReason ? `: ${humanize(row.staleReason)}` : ''}
        </span>
      )}
      {row.servedFromReleaseId && (
        <span className="badge badge-served" data-served-from={row.servedFromReleaseId}>
          served from release {seq !== null && seq !== undefined ? `#${seq}` : <span className="mono">{row.servedFromReleaseId.slice(0, 8)}…</span>}
        </span>
      )}
      {row.proposalId && (
        <>
          {' '}
          <Link to={`/acquisition?proposal=${encodeURIComponent(row.proposalId)}#proposal-${row.proposalId}`}>repair proposal</Link>
        </>
      )}
    </span>
  );
}

const rowColumns = (releaseSeq: (id: string) => number | null | undefined): ColumnDef<Row, unknown>[] => [
  { header: 'Period', accessorKey: 'period' },
  { header: 'Value', id: 'value', cell: (c) => <ValueView value={c.row.original.value} showBasis /> },
  { header: 'Uncertainty', id: 'unc', cell: (c) => <UncertaintyPanel uncertainty={c.row.original.uncertainty} compact /> },
  { header: 'Source', accessorKey: 'source' },
  {
    header: 'Notes',
    id: 'notes',
    cell: (c) => (
      <>
        {c.row.original.detail}
        {c.row.original.flags.length > 0 && (
          <span className="source-flag" data-testid="source-flag">
            {' '}
            · {c.row.original.flags.join('; ')}
          </span>
        )}
        {c.row.original.flagsTruncated && (
          <span className="field-hint" data-testid="flags-truncated">
            {' '}
            · provider flags truncated to their size bound; the full record is on the observation (Trace)
          </span>
        )}{' '}
        <StaleNotes row={c.row.original} releaseSeq={releaseSeq} />
      </>
    ),
  },
  {
    header: 'Provenance',
    id: 'prov',
    cell: (c) => <ProvenanceButton kind={c.row.original.provKind} id={c.row.original.provId} label="Trace" />,
  },
];

function beliefRows(data: BeliefEstimate[], baselineReleaseId: string | null, concept: string, origin: string | null): Row[] {
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
      observationId: e.observation_id ?? null,
      flags: sourceFlagNotes(e.source_flags, { concept }),
      flagsTruncated: !!e.source_flags_truncated,
      origin,
      stale: !!e.stale,
      staleReason: e.stale_reason ?? null,
      proposalId: e.proposal_id ?? null,
      servedFromReleaseId: e.served_from_release_id && e.served_from_release_id !== baselineReleaseId ? e.served_from_release_id : null,
    };
  });
}

function evidenceRows(points: TimeseriesPoint[], concept: string): Row[] {
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
    observationId: p.observation_id,
    flags: sourceFlagNotes(p.source_flags, { concept }),
    flagsTruncated: !!p.source_flags_truncated,
    origin: p.origin ?? null,
    stale: false,
    staleReason: null,
    proposalId: null,
    servedFromReleaseId: null,
  }));
}

interface ConceptPanelProps {
  iso3: string;
  concept: string;
  title: string;
  definition?: ConceptOut;
  /** Reports the origins (live / test_fixture) of the records this panel shows. */
  onOrigins?: (concept: string, origins: string[]) => void;
}

function ConceptPanel({ iso3, concept, title, definition, onOrigins }: ConceptPanelProps) {
  const [mode, setMode] = useState<'belief' | 'evidence'>('belief');
  const beliefs = useApi((s) => getBeliefs({ subject_id: iso3, concept, limit: 1000 }, s), [iso3, concept], mode === 'belief');
  const series = useApi((s) => getTimeseries({ subject_id: iso3, concept }, s), [iso3, concept], mode === 'evidence');
  const state = mode === 'belief' ? beliefs : series;
  // Provider source flags now travel on the estimate / point itself (no extra /v1/observations call).
  const rows = useMemo(
    () =>
      mode === 'belief'
        ? beliefs.data
          ? beliefRows(beliefs.data.data, beliefs.data.baseline_release?.id ?? null, concept, beliefs.data.origin ?? null)
          : []
        : series.data
          ? evidenceRows(series.data.data.points, concept)
          : [],
    [mode, beliefs.data, series.data, concept],
  );
  const sorted = useMemo(() => [...rows].sort((a, b) => (a.year ?? 0) - (b.year ?? 0)), [rows]);
  // Sequences of earlier releases that served stale rows ("served from release #N").
  const servedIds = useMemo(() => [...new Set(rows.map((r) => r.servedFromReleaseId).filter((x): x is string => !!x))].sort(), [rows]);
  const served = useApi(
    async (s) => {
      const out: Record<string, number | null> = {};
      await Promise.all(
        servedIds.map(async (id) => {
          try {
            out[id] = (await getBeliefRelease(id, s)).data.sequence ?? null;
          } catch {
            out[id] = null;
          }
        }),
      );
      return out;
    },
    [servedIds.join(',')],
    servedIds.length > 0,
  );
  const columns = useMemo(() => rowColumns((id) => served.data?.[id]), [served.data]);
  // Origins of the records actually shown: the panel envelope and the rows' own origins.
  const shownOrigins = useMemo(() => {
    const o: string[] = [];
    if (state.data?.origin && state.data.origin !== 'none') o.push(state.data.origin);
    for (const r of rows) if (r.origin && r.origin !== 'none') o.push(r.origin);
    return [...new Set(o)].sort();
  }, [state.data, rows]);
  const originKey = shownOrigins.join(',');
  useEffect(() => {
    onOrigins?.(concept, originKey ? originKey.split(',') : []);
  }, [onOrigins, concept, originKey]);
  const glyphs = useMemo(() => glyphKindsOf(rows), [rows]);
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
              {latest.flags.length > 0 && <span className="source-flag"> · {latest.flags.join('; ')}</span>}{' '}
              <StaleNotes row={latest} releaseSeq={(id) => served.data?.[id]} />
            </p>
          )}
          {missingPeriods.length > 0 && (
            <p className="muted">Missing (shown as gaps, not zero): {missingPeriods.join(', ')}</p>
          )}
          <TimeSeriesChart points={chartPoints} label={`${title} by year for ${iso3}; table follows`} yLabel={unitLabel(unit) || 'value'} />
          <UncertaintyLegend present={glyphs} />
          <DataTable caption={`${title} — ${iso3} (${mode === 'belief' ? 'selected estimates' : 'latest source revisions'})`} columns={columns} rows={sorted} rowKey={(r) => r.key} />
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

  // The entity envelope reports origin "none" (it holds no records itself); the banner shows the
  // origin of the records the panels below actually display instead.
  const [panelOrigins, setPanelOrigins] = useState<Record<string, string[]>>({});
  const onOrigins = useCallback(
    (concept: string, origins: string[]) => {
      const k = `${iso3}|${concept}`;
      setPanelOrigins((prev) => (prev[k]?.join(',') === origins.join(',') ? prev : { ...prev, [k]: origins }));
    },
    [iso3],
  );
  const dataOrigin = useMemo(
    () => combineOrigins(Object.entries(panelOrigins).flatMap(([k, o]) => (k.startsWith(`${iso3}|`) ? o : []))),
    [panelOrigins, iso3],
  );

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
      <ContextBanner meta={entity.data} scope="Country profile" dataOrigin={dataOrigin} />
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

      {ent && <MirrorPanel entity={iso3} entityId={ent.id} countryLabel={ent.label} />}

      {PROFILE_PANELS.map((p) => (
        <ConceptPanel
          key={`${iso3}-${p.concept}`}
          iso3={iso3}
          concept={p.concept}
          title={p.title}
          definition={concepts.get(p.concept)}
          onOrigins={onOrigins}
        />
      ))}
    </>
  );
}
