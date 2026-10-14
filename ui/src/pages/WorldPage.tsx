import { useMemo } from 'react';
import { Link } from 'react-router';
import type { ColumnDef } from '@tanstack/react-table';
import { getCoverage, getEntity, getEvents, getEvidence, getGaps, getLocations, getSources } from '../api/client';
import type { Gap, RequirementCoverage, SourceOut } from '../api/types';
import { ContextBanner } from '../components/ContextBanner';
import { DataTable } from '../components/DataTable';
import { EnvelopeDetails } from '../components/EnvelopeDetails';
import { MapView } from '../components/MapView';
import { catalogueKeys, computeAvailability } from '../lib/availability';
import { Empty, ErrorState, Loading } from '../components/StateViews';
import { formatDate, formatDateTime, humanize } from '../lib/format';
import { useApi } from '../lib/useApi';

function daysSince(iso: string | null | undefined): number | null {
  if (!iso) return null;
  const t = new Date(iso).getTime();
  if (Number.isNaN(t)) return null;
  return Math.floor((Date.now() - t) / 86_400_000);
}

const coverageColumns: ColumnDef<RequirementCoverage, unknown>[] = [
  { header: 'Requirement', accessorKey: 'requirement_id' },
  { header: 'Concept', accessorKey: 'concept', cell: (c) => <span className="mono">{String(c.getValue())}</span> },
  { header: 'Geography', accessorFn: (r) => r.geography.join(', ') },
  { header: 'Period', accessorFn: (r) => (r.period.length === 2 ? `${r.period[0]}–${r.period[1]}` : r.period.join(', ')) },
  {
    header: 'Available / required',
    accessorKey: 'availability_ratio',
    cell: (c) => <span className="num">{String(c.getValue())}</span>,
  },
  { header: 'Semantically compatible', accessorKey: 'semantic_ratio' },
  {
    header: 'Fresh',
    accessorKey: 'freshness_ratio',
    cell: (c) => (
      <span title={`within ${c.row.original.acceptable_freshness_days} days`}>
        {String(c.getValue())} <span className="muted">(≤{c.row.original.acceptable_freshness_days} d)</span>
      </span>
    ),
  },
  { header: 'Typed missing', accessorKey: 'typed_missing' },
  { header: 'Absent', accessorKey: 'absent' },
];

const sourceColumns: ColumnDef<SourceOut, unknown>[] = [
  { header: 'Source', accessorKey: 'source_key' },
  { header: 'Title', accessorKey: 'title' },
  { header: 'Records', accessorKey: 'observations' },
  {
    header: 'Last retrieved',
    accessorKey: 'last_retrieved_at',
    cell: (c) => {
      const v = c.getValue() as string | null;
      if (!v) return <span className="muted">never retrieved</span>;
      const d = daysSince(v);
      return (
        <span>
          {formatDateTime(v)} {d !== null && <span className="muted">({d} d ago)</span>}
        </span>
      );
    },
  },
  {
    header: 'Licence',
    accessorFn: (r) => String((r.rights as Record<string, unknown>)?.licence ?? (r.rights as Record<string, unknown>)?.license ?? '—'),
  },
];

const gapColumns: ColumnDef<Gap, unknown>[] = [
  { header: 'Entity', accessorKey: 'entity_key', cell: (c) => <Link to={`/country/${String(c.getValue())}`}>{String(c.getValue())}</Link> },
  { header: 'Concept', accessorKey: 'concept', cell: (c) => <span className="mono">{String(c.getValue())}</span> },
  { header: 'Period', accessorKey: 'period' },
  { header: 'Gap', accessorKey: 'gap_kind', cell: (c) => humanize(String(c.getValue())) },
  { header: 'Reason', accessorKey: 'reason' },
  { header: 'Action', accessorKey: 'action' },
];

export default function WorldPage() {
  const coverage = useApi((s) => getCoverage(undefined, s), []);
  const gaps = useApi((s) => getGaps({ limit: 1000 }, s), []);
  const sources = useApi((s) => getSources(undefined, s), []);
  const locations = useApi((s) => getLocations(undefined, s), []);
  const events = useApi((s) => getEvents({ limit: 20 }, s), []);
  const evidence = useApi((s) => getEvidence({ limit: 50 }, s), []);

  const keys = useMemo(() => catalogueKeys(coverage.data?.data), [coverage.data]);

  // Resolve catalogue geography keys (ISO3) to REVA entity ids for the map.
  const entityIds = useApi(
    async (s) => {
      const out: Record<string, string> = {};
      await Promise.all(
        keys.map(async (k) => {
          try {
            out[k] = (await getEntity(k, s)).data.id;
          } catch {
            /* unresolved key: left out of the map colouring */
          }
        }),
      );
      return out;
    },
    [keys.join(',')],
    keys.length > 0,
  );

  const availability = useMemo(
    () => computeAvailability(coverage.data?.data, gaps.data?.data, entityIds.data),
    [coverage.data, gaps.data, entityIds.data],
  );

  const recentEvidence = useMemo(
    () =>
      [...(evidence.data?.data ?? [])]
        .filter((e) => e.recorded_at)
        .sort((a, b) => String(b.recorded_at).localeCompare(String(a.recorded_at)))
        .slice(0, 10),
    [evidence.data],
  );

  const cov = coverage.data;
  const asOf = cov?.knowledge_cutoff ?? cov?.baseline_release?.knowledge_cutoff;

  return (
    <>
      <h1>World overview</h1>
      <ContextBanner meta={cov} scope="Catalogue-relative coverage" />

      <section className="panel" aria-labelledby="coverage-h">
        <h2 id="coverage-h">Domain coverage</h2>
        {coverage.status === 'loading' && <Loading what="coverage" />}
        {coverage.status === 'error' && <ErrorState error={coverage.error} what="Coverage" onRetry={coverage.reload} />}
        {cov && (
          <>
            <p>
              <strong>{cov.data.statement}</strong>
            </p>
            <p className="muted">
              Catalogue <span className="mono">{cov.data.catalogue_version}</span>
              {cov.baseline_release ? (
                <>
                  {' '}
                  · release #{cov.baseline_release.sequence ?? '—'} · knowledge cutoff {formatDateTime(asOf)}
                </>
              ) : (
                ' · no published release'
              )}
              . Coverage is relative to this finite catalogue, never to reality.
            </p>
            <p>
              Totals: <span className="num">{cov.data.totals.available}</span> of{' '}
              <span className="num">{cov.data.totals.denominator}</span> required cells available;{' '}
              {cov.data.totals.semantically_compatible} semantically compatible; {cov.data.totals.fresh} fresh.
            </p>
            <DataTable
              caption={`Coverage by requirement (as of ${formatDate(asOf)})`}
              columns={coverageColumns}
              rows={cov.data.requirements}
              rowKey={(r) => r.requirement_id}
            />
            <EnvelopeDetails meta={cov} />
          </>
        )}
      </section>

      <section className="panel" aria-labelledby="map-h">
        <h2 id="map-h">Data availability map</h2>
        {locations.status === 'loading' && <Loading what="display geometry" />}
        {locations.status === 'error' && <ErrorState error={locations.error} what="Display geometry" onRetry={locations.reload} />}
        {locations.data && locations.data.data.features.length === 0 && (
          <Empty what="display geometry">
            <p>Natural Earth display geometry has not been ingested. Run the naturalearth connector.</p>
          </Empty>
        )}
        {locations.data && locations.data.data.features.length > 0 && (
          <>
            <MapView
              collection={locations.data.data}
              availability={availability}
              label="World map of display geometries coloured by REVA data availability for catalogue countries"
            />
            <p className="map-note">{locations.data.data.display_note}</p>
            {locations.data.data.truncated && <p className="bound-flag">Feature list truncated by the API bound.</p>}
            <details>
              <summary>Feature status table (text alternative)</summary>
              <DataTable
                caption="Display features and match status"
                columns={[
                  { header: 'Feature', accessorFn: (f) => f.properties.name ?? f.properties.feature_key },
                  { header: 'Entity', accessorFn: (f) => f.properties.entity_label ?? '—' },
                  { header: 'Match status', accessorFn: (f) => humanize(f.properties.match_status) },
                  {
                    header: 'Availability',
                    accessorFn: (f) =>
                      f.properties.entity_id ? humanize(availability[f.properties.entity_id] ?? 'not_in_catalogue') : 'no entity match',
                  },
                ]}
                rows={locations.data.data.features}
                rowKey={(f) => f.id}
              />
            </details>
          </>
        )}
      </section>

      <div className="grid">
        <section className="panel" aria-labelledby="fresh-h">
          <h2 id="fresh-h">Source freshness</h2>
          {sources.status === 'loading' && <Loading what="sources" />}
          {sources.status === 'error' && <ErrorState error={sources.error} what="Sources" onRetry={sources.reload} />}
          {sources.data &&
            (sources.data.data.length === 0 ? (
              <Empty what="sources" />
            ) : (
              <DataTable caption="Registered sources and last retrieval" columns={sourceColumns} rows={sources.data.data} rowKey={(r) => r.id} />
            ))}
        </section>

        <section className="panel" aria-labelledby="events-h">
          <h2 id="events-h">Recent evidence</h2>
          {evidence.status === 'loading' && <Loading what="evidence" />}
          {evidence.status === 'error' && <ErrorState error={evidence.error} what="Evidence" onRetry={evidence.reload} />}
          {evidence.data &&
            (recentEvidence.length === 0 ? (
              <Empty what="dated evidence items" />
            ) : (
              <ul>
                {recentEvidence.map((e) => (
                  <li key={e.id}>
                    <span className="tag">{humanize(e.kind)}</span> {e.title}{' '}
                    <span className="muted">recorded {formatDate(e.recorded_at)}</span>
                  </li>
                ))}
              </ul>
            ))}
          <h3>Recorded occurrences</h3>
          {events.status === 'error' && <ErrorState error={events.error} what="Events" onRetry={events.reload} />}
          {events.data &&
            (events.data.data.length === 0 ? (
              <p className="muted">No real-world occurrences are recorded in REVA yet.</p>
            ) : (
              <ul>
                {events.data.data.map((ev) => (
                  <li key={ev.entity_id}>
                    {ev.label} <span className="muted">({humanize(ev.event_type)}, {ev.support.period_label ?? 'time not stated'})</span>
                  </li>
                ))}
              </ul>
            ))}
        </section>
      </div>

      <section className="panel" aria-labelledby="gaps-h">
        <h2 id="gaps-h">Knowledge and model-input gaps</h2>
        {gaps.status === 'loading' && <Loading what="gaps" />}
        {gaps.status === 'error' && <ErrorState error={gaps.error} what="Gaps" onRetry={gaps.reload} />}
        {gaps.data &&
          (gaps.data.data.length === 0 ? (
            <Empty what="gaps">
              <p>No catalogue gaps are open for the pinned release.</p>
            </Empty>
          ) : (
            <DataTable
              caption={`Open gaps (${gaps.data.data.length}${gaps.data.next_cursor ? '+' : ''})`}
              columns={gapColumns}
              rows={gaps.data.data}
              rowKey={(g, i) => `${g.requirement_id}-${g.entity_key}-${g.period}-${i}`}
            />
          ))}
      </section>
    </>
  );
}
