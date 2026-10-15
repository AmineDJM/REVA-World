import { useMemo, useState } from 'react';
import { Link, useSearchParams } from 'react-router';
import { getEntity, getJobs, getLocations, getRelations, getScenarios, getSimulation } from '../api/client';
import type { Job, Scenario } from '../api/types';
import { MODEL_KEYS } from '../api/v2types';
import { ContextBanner } from '../components/ContextBanner';
import { ExposureMap, NOT_IN_RUN_COLOR, NO_ENTITY_COLOR, type FeatureStyle, type MapArc } from '../components/ExposureMap';
import { MirrorPanel, MIRROR_LABEL } from '../components/MirrorPanel';
import { MissingValue } from '../components/MissingValue';
import { ProvenanceButton } from '../components/ProvenanceDrawer';
import { Empty, ErrorState, Loading } from '../components/StateViews';
import { UncertaintyGlyph } from '../components/UncertaintyGlyph';
import { EXPOSURE_SCALE, classify, legendRows } from '../lib/colorScale';
import { displayDecimal, formatDateTime, humanize, shortId } from '../lib/format';
import { useApi } from '../lib/useApi';
import {
  anchorsByEntity,
  arcWidth,
  exposureRows,
  greatCircle,
  mergeReleaseRelations,
  reportedPartnerFlows,
  runCountryStatuses,
  type CountryRunStatus,
  type FlowRow,
} from '../lib/worldmap';

const STATE_TEXT: Record<CountryRunStatus['state'], string> = {
  computed: 'computed',
  missing_input: 'not computed: missing input',
  skipped: 'skipped: inputs incomplete',
  failed: 'not computed: failed',
  not_reported: 'no result reported',
};

function fmtPct(v: string): string {
  const d = displayDecimal(v, 'unit.percent');
  return `${v.trim().startsWith('-') ? '' : '+'}${d.text} %`;
}

/** Bounded-concurrency resolution of ISO3 keys to entity ids (only for countries without a result row). */
async function resolveIsos(isos: string[], signal: AbortSignal): Promise<Record<string, { id: string; label: string }>> {
  const out: Record<string, { id: string; label: string }> = {};
  const queue = [...isos];
  const worker = async () => {
    while (queue.length) {
      const iso = queue.shift()!;
      try {
        const e = await getEntity(iso, signal);
        out[iso] = { id: e.data.id, label: e.data.label };
      } catch {
        /* unresolved: listed in the table, not drawn */
      }
    }
  };
  await Promise.all(Array.from({ length: Math.min(6, queue.length) }, worker));
  return out;
}

export interface CrudeRunOption {
  job: Job;
  scenario: Scenario | null;
}

export function crudeRunOptions(jobs: Job[], scenarios: Scenario[]): CrudeRunOption[] {
  const byId = new Map(scenarios.map((s) => [s.id, s]));
  return jobs
    .filter((j) => j.status === 'succeeded')
    .map((j) => ({ job: j, scenario: byId.get(String((j.payload as Record<string, unknown>)?.scenario_id ?? '')) ?? null }))
    .filter((o) => !o.scenario || o.scenario.spec.model_key === MODEL_KEYS.crude);
}

export default function MapPage() {
  const [params, setParams] = useSearchParams();
  const simParam = params.get('sim');
  const focusParam = params.get('focus');
  const flow = params.get('flow') === 'X' ? 'X' : 'M';

  const locations = useApi((s) => getLocations(undefined, s), []);
  const jobs = useApi((s) => getJobs({ job_type: 'simulation', limit: 100 }, s), []);
  const scenarios = useApi((s) => getScenarios({ limit: 500 }, s), []);
  const options = useMemo(() => crudeRunOptions(jobs.data?.data ?? [], scenarios.data?.data ?? []), [jobs.data, scenarios.data]);
  const simId = simParam ?? options[0]?.job.id ?? null;

  const sim = useApi((s) => getSimulation(simId!, s), [simId], !!simId);
  const st = sim.data?.data;
  const statuses = useMemo(() => runCountryStatuses(st), [st]);
  const rows = useMemo(() => exposureRows(st?.results ?? []), [st]);
  const uncomputed = useMemo(() => Object.values(statuses).filter((s) => s.state !== 'computed'), [statuses]);
  // Entity ids of countries without a result: stated by the API (skipped_countries[].subject_id) or resolved by ISO3.
  const toResolve = uncomputed.filter((u) => !u.subjectId).map((u) => u.iso3);
  const looked = useApi((s) => resolveIsos(toResolve, s), [toResolve.join(',')], toResolve.length > 0);
  const resolved = useMemo(() => {
    const out: Record<string, { id: string; label: string }> = { ...(looked.data ?? {}) };
    const labels = new Map<string | undefined, string | undefined>((st?.results ?? []).map((r) => [r.subject?.id, r.subject?.label]));
    for (const f of locations.data?.data.features ?? []) if (f.properties.entity_id && f.properties.entity_label) labels.set(f.properties.entity_id, f.properties.entity_label);
    for (const u of uncomputed) if (u.subjectId) out[u.iso3] = { id: u.subjectId, label: labels.get(u.subjectId) ?? looked.data?.[u.iso3]?.label ?? u.iso3 };
    return { data: out };
  }, [looked.data, uncomputed, st, locations.data]);

  const focusId = focusParam ?? rows[0]?.entityId ?? null;
  const focusRow = rows.find((r) => r.entityId === focusId) ?? null;
  const focusGap = uncomputed.find((u) => resolved.data?.[u.iso3]?.id === focusId) ?? null;
  const focusLabel = focusRow?.label ?? (focusGap ? (resolved.data?.[focusGap.iso3]?.label ?? focusGap.iso3) : null);
  const refYear = typeof st?.scenario?.spec?.reference_year === 'number' ? (st.scenario.spec.reference_year as number) : null;
  const cutoff = st?.scenario?.knowledge_cutoff ?? null;

  // Release-consistent view: relations pinned to the run's baseline release; where the release
  // carries no flow value, evidence as known at the scenario cutoff fills it (marked per row).
  const releaseId = st?.scenario?.baseline_release?.id ?? null;
  const relRelease = useApi(
    (s) => getRelations({ entity_id: focusId!, layer: 'trade.crude_petroleum', period: refYear ?? undefined, release_id: releaseId!, max_edges: 500 }, s),
    [focusId, refYear, releaseId],
    !!focusId && !!releaseId,
  );
  const needsFallback = !!relRelease.data && relRelease.data.data.relations.some((r) => !r.value);
  const relEvidence = useApi(
    (s) => getRelations({ entity_id: focusId!, layer: 'trade.crude_petroleum', period: refYear ?? undefined, as_of: cutoff ?? undefined, max_edges: 500 }, s),
    [focusId, refYear, cutoff],
    !!focusId && !!st && (needsFallback || !releaseId),
  );
  const relations = {
    status: relRelease.status === 'error' ? relRelease.status : releaseId ? relRelease.status : relEvidence.status,
    error: relRelease.error ?? relEvidence.error,
    reload: () => {
      relRelease.reload();
      relEvidence.reload();
    },
    data: releaseId ? relRelease.data : relEvidence.data,
  };
  const mergedRelations = useMemo(
    () =>
      releaseId
        ? relRelease.data
          ? mergeReleaseRelations(relRelease.data.data.relations, relEvidence.data?.data.relations)
          : null
        : (relEvidence.data?.data.relations ?? null),
    [releaseId, relRelease.data, relEvidence.data],
  );
  const partners = useMemo(
    () => (mergedRelations && focusId ? reportedPartnerFlows(mergedRelations, focusId, flow) : null),
    [mergedRelations, focusId, flow],
  );

  const features = locations.data?.data.features ?? [];
  const anchors = useMemo(() => anchorsByEntity(features), [features]);

  const styles = useMemo(() => {
    const out: Record<string, FeatureStyle> = {};
    const byEntity = new Map(rows.map((r) => [r.entityId, r]));
    const gapByEntity = new Map<string, CountryRunStatus>();
    for (const u of uncomputed) {
      const id = resolved.data?.[u.iso3]?.id;
      if (id) gapByEntity.set(id, u);
    }
    for (const f of features) {
      const name = f.properties.name ?? f.properties.entity_label ?? f.properties.feature_key;
      const eid = f.properties.entity_id;
      if (!eid) {
        out[f.id] = { state: 'no_entity', color: NO_ENTITY_COLOR, tooltip: `${name}: no reviewed entity match (display geometry only)` };
        continue;
      }
      const r = byEntity.get(eid);
      if (r) {
        const c = classify(r.value, EXPOSURE_SCALE);
        out[f.id] = { state: 'value', color: c?.color, tooltip: `${r.label}: ${fmtPct(r.value)} of baseline GDP (L1, simulation)` };
        continue;
      }
      const g = gapByEntity.get(eid);
      if (g) {
        out[f.id] = {
          state: 'gap',
          tooltip: `${name}: ${STATE_TEXT[g.state]}${g.reasons.length ? ` — ${g.reasons.slice(0, 3).join('; ')}${g.reasons.length > 3 ? ` (+${g.reasons.length - 3} more)` : ''}` : ''}. Not zero.`,
        };
        continue;
      }
      out[f.id] = { state: 'not_in_run', color: NOT_IN_RUN_COLOR, tooltip: `${name}: not in this run` };
    }
    return out;
  }, [features, rows, uncomputed, resolved.data]);

  const { arcs, partnerPoints, unmappedPartners } = useMemo(() => {
    const arcs: MapArc[] = [];
    const partnerPoints: { id: string; coordinates: [number, number]; label: string }[] = [];
    const unmapped: FlowRow[] = [];
    const from = focusId ? anchors.get(focusId) : undefined;
    const drawable = (partners?.countries ?? []).filter((p) => p.value !== null && anchors.has(p.partnerId) && !!from);
    const max = Math.max(0, ...drawable.map((p) => Number(p.value)));
    for (const p of partners?.countries ?? []) {
      const to = anchors.get(p.partnerId);
      if (!from || !to || p.value === null) {
        unmapped.push(p);
        continue;
      }
      // Imports flow partner -> country; exports country -> partner.
      const coords = flow === 'M' ? greatCircle(to, from) : greatCircle(from, to);
      arcs.push({ id: p.relationId, coordinates: coords, width: arcWidth(p.value, max), label: p.partnerLabel });
      partnerPoints.push({ id: p.partnerId, coordinates: to, label: p.partnerLabel });
    }
    return { arcs, partnerPoints, unmappedPartners: unmapped };
  }, [partners, anchors, focusId, flow]);

  const setParam = (k: string, v: string | null) => {
    const next = new URLSearchParams(params);
    if (v === null) next.delete(k);
    else next.set(k, v);
    setParams(next, { replace: true });
  };

  const countryOptions = useMemo(() => {
    const list = rows.map((r) => ({ id: r.entityId, label: r.label, note: fmtPct(r.value) }));
    for (const u of uncomputed) {
      const r = resolved.data?.[u.iso3];
      if (r) list.push({ id: r.id, label: r.label, note: STATE_TEXT[u.state] });
    }
    return list.sort((a, b) => a.label.localeCompare(b.label));
  }, [rows, uncomputed, resolved.data]);

  const loadingRuns = jobs.status === 'loading' || scenarios.status === 'loading';

  return (
    <>
      <h1>World map: crude border-price exposure</h1>
      <ContextBanner
        meta={sim.data ?? { context_mode: 'simulation', modality: 'assumption_conditioned_scenario', origin: 'none' }}
        simulation
        scope={st ? `Run ${shortId(simId)} — choropleth of exposure_percent_baseline_gdp (L1)` : 'No run selected'}
        referenceTime={sim.data?.valid_time ? undefined : refYear ? `${refYear} (scenario reference year; one annual accounting period)` : undefined}
      />

      <section className="panel" aria-labelledby="run-h">
        <h2 id="run-h">Run</h2>
        {loadingRuns && <Loading what="simulation runs" />}
        {jobs.status === 'error' && <ErrorState error={jobs.error} what="Simulation runs" onRetry={jobs.reload} />}
        {!loadingRuns && options.length === 0 && (
          <Empty what="succeeded crude-exposure runs">
            <p>
              No succeeded crude-exposure run exists yet. Create one on the <Link to="/simulation">Simulation</Link> screen or with the{' '}
              <Link to="/compose">scenario composer</Link>; the map colours countries only from a run's own results.
            </p>
          </Empty>
        )}
        {options.length > 0 && (
          <label>
            Simulation run
            <select value={simId ?? ''} onChange={(e) => setParams({ sim: e.target.value })}>
              {!options.some((o) => o.job.id === simId) && simId && <option value={simId}>{shortId(simId)} (from link)</option>}
              {options.map((o) => (
                <option key={o.job.id} value={o.job.id}>
                  {shortId(o.job.id)} · {o.scenario?.title ?? 'scenario not listed'} · {String(o.scenario?.spec?.fidelity ?? '—')} ·{' '}
                  {Array.isArray(o.scenario?.spec?.countries) ? `${(o.scenario!.spec.countries as unknown[]).length} countries` : '—'} ·{' '}
                  {formatDateTime(o.job.finished_at ?? o.job.created_at)}
                </option>
              ))}
            </select>
          </label>
        )}
        {sim.status === 'error' && <ErrorState error={sim.error} what="The simulation" onRetry={sim.reload} />}
        {sim.data?.warnings?.some((w) => w.code === 'results_truncated') && (
          <p className="bound-flag" role="status" data-testid="results-truncated">
            The API returned only part of this run's result rows; countries without a returned row are drawn as "no result reported", not as
            zero.
          </p>
        )}
        {st && (
          <p className="muted">
            Scenario <span className="mono">{shortId(st.scenario?.id)}</span> · baseline release #{st.scenario?.baseline_release?.sequence ?? '—'} · cutoff{' '}
            {formatDateTime(st.scenario?.knowledge_cutoff)} · outcome {st.outcome ? humanize(st.outcome.status) : 'not finished'} ·{' '}
            {rows.length} countr{rows.length === 1 ? 'y' : 'ies'} computed, {uncomputed.length} without a result ·{' '}
            <Link to={`/runs/${simId}`}>full results</Link>
          </p>
        )}
      </section>

      <section className="panel map-panel" aria-labelledby="map2-h">
        <h2 id="map2-h">Exposure, % of same-year baseline GDP (L1, fixed quantities)</h2>
        {locations.status === 'loading' && <Loading what="display geometry" />}
        {locations.status === 'error' && <ErrorState error={locations.error} what="Display geometry" onRetry={locations.reload} />}
        {st && features.length > 0 && (
          <>
            <div className="map-controls">
              <label>
                Country for partner arcs
                <select value={focusId ?? ''} onChange={(e) => setParam('focus', e.target.value || null)}>
                  {countryOptions.map((c) => (
                    <option key={c.id} value={c.id}>
                      {c.label} — {c.note}
                    </option>
                  ))}
                </select>
              </label>
              <fieldset className="seg">
                <legend>Partner flows</legend>
                <label>
                  <input type="radio" name="flow" checked={flow === 'M'} onChange={() => setParam('flow', 'M')} /> Imports (origins)
                </label>
                <label>
                  <input type="radio" name="flow" checked={flow === 'X'} onChange={() => setParam('flow', 'X')} /> Exports (destinations)
                </label>
              </fieldset>
            </div>
            <ExposureMap
              collection={locations.data!.data}
              styles={styles}
              arcs={arcs}
              partnerPoints={partnerPoints}
              selectedEntityId={focusId}
              onSelectEntity={(id) => setParam('focus', id)}
              label={`World map of the run's crude exposure as % of baseline GDP; ${rows.length} countries coloured, ${uncomputed.length} hatched without complete inputs; ${arcs.length} reported partner arcs for ${focusLabel ?? 'no country'}. The table below lists the same data.`}
            />
            <ExposureLegend />
          </>
        )}
        {!st && sim.status !== 'loading' && options.length > 0 && <p className="muted">Select a run.</p>}
        {sim.status === 'loading' && <Loading what="run results" />}
      </section>

      {st && (
        <div className="grid">
          <section className="panel" aria-labelledby="countries-h">
            <h2 id="countries-h">Countries in this run</h2>
            <div className="table-wrap">
              <table className="data-table" data-testid="run-countries">
                <caption>Exposure by country (text alternative to the map); hatched = no complete inputs, never zero</caption>
                <thead>
                  <tr>
                    <th scope="col">Country</th>
                    <th scope="col">Exposure, % of GDP (L1)</th>
                    <th scope="col">State</th>
                  </tr>
                </thead>
                <tbody>
                  {rows.map((r) => (
                    <tr key={r.entityId} aria-selected={r.entityId === focusId || undefined}>
                      <th scope="row">
                        <button type="button" className="link-button" onClick={() => setParam('focus', r.entityId)} aria-pressed={r.entityId === focusId}>
                          {r.label}
                        </button>
                      </th>
                      <td>
                        <span className="swatch" style={{ background: classify(r.value, EXPOSURE_SCALE)?.color }} aria-hidden="true" />{' '}
                        <span className="num" title={`exact: ${r.value}`} data-exact={r.value}>
                          {fmtPct(r.value)}
                        </span>{' '}
                        <ProvenanceButton kind="sim_result" id={r.result.id} label="Trace" />
                      </td>
                      <td>computed</td>
                    </tr>
                  ))}
                  {uncomputed.map((u) => (
                    <tr key={u.iso3} data-gap-country={u.iso3}>
                      <th scope="row">
                        {resolved.data?.[u.iso3] ? (
                          <button type="button" className="link-button" onClick={() => setParam('focus', resolved.data![u.iso3]!.id)}>
                            {resolved.data[u.iso3]!.label}
                          </button>
                        ) : (
                          u.iso3
                        )}
                      </th>
                      <td>
                        <MissingValue reason={u.state === 'missing_input' ? 'missing_input' : u.state} />
                      </td>
                      <td>
                        {STATE_TEXT[u.state]}
                        {u.reasons.length > 0 && (
                          <ul className="reasons">
                            {u.reasons.map((x, i) => (
                              <li key={i}>{x}</li>
                            ))}
                          </ul>
                        )}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </section>

          <section className="panel" aria-labelledby="partners-h">
            <h2 id="partners-h">
              {flow === 'M' ? 'Reported origins' : 'Reported destinations'} for {focusLabel ?? '—'}
            </h2>
            <p className="field-hint">
              Bilateral flows {focusLabel ?? 'the country'} itself reports for {refYear ?? 'the reference year'}, from the run's pinned baseline
              release (evidence as of the scenario cutoff where the release records no value; marked per row). They
              are context: the crude model uses World totals, not these rows. "World" is never drawn as an arc; composite partner areas are listed,
              not mapped.
            </p>
            {relations.status === 'loading' && <Loading what="partner flows" />}
            {relations.status === 'error' && <ErrorState error={relations.error} what="Partner flows" onRetry={relations.reload} />}
            {relations.data?.data.truncated && <p className="bound-flag">Relation list bounded at {relations.data.data.max_edges} edges.</p>}
            {partners && partners.countries.length + partners.composites.length === 0 && (
              <p className="muted" role="status">
                {focusLabel} reports no bilateral {flow === 'M' ? 'imports' : 'exports'} for this year in the evidence held. Absence of a report, not a
                zero flow.
              </p>
            )}
            {partners && partners.countries.length > 0 && (
              <PartnerTable rows={partners.countries} arcs={arcs} unmapped={unmappedPartners.length} />
            )}
            {partners && partners.composites.length > 0 && (
              <div data-testid="composite-partners">
                <h3>Composite partner areas (listed, not mapped)</h3>
                <ul>
                  {partners.composites.map((p) => (
                    <li key={p.relationId}>
                      {p.partnerLabel} <span className="muted">({humanize(p.partnerKind.replace(/^entity\./, ''))})</span>:{' '}
                      {p.value === null ? <MissingValue reason={p.missingReason ?? 'unavailable'} /> : <span className="num">{displayDecimal(p.value, 'unit.usd').text} US$</span>}
                    </li>
                  ))}
                </ul>
              </div>
            )}
            {partners && partners.self.length > 0 && (
              <p className="muted" data-testid="self-partner">
                {focusLabel} also reports {flow === 'M' ? 'imports from' : 'exports to'} itself (re-{flow === 'M' ? 'imports' : 'exports'}):{' '}
                {partners.self.map((p) => (p.value === null ? 'missing' : `${displayDecimal(p.value, 'unit.usd').text} US$`)).join(', ')}. Listed, not drawn.
              </p>
            )}
            {partners && partners.world.length > 0 && (
              <p className="muted">{partners.world.length} partner-World row(s) excluded from arcs (a World total is not a partner).</p>
            )}
          </section>
        </div>
      )}

      {st && focusId && focusLabel && (focusGap || rows.length === 0) && (
        <MirrorPanel entity={focusId} entityId={focusId} countryLabel={focusLabel} asOf={cutoff} />
      )}
      {st && focusId && focusLabel && !focusGap && rows.length > 0 && (
        <details className="panel">
          <summary>Mirror view for {focusLabel} ({MIRROR_LABEL})</summary>
          <MirrorPanel entity={focusId} entityId={focusId} countryLabel={focusLabel} asOf={cutoff} headingLevel={3} />
        </details>
      )}
    </>
  );
}

const PARTNER_PREVIEW = 12;

function PartnerTable({ rows, arcs, unmapped }: { rows: FlowRow[]; arcs: MapArc[]; unmapped: number }) {
  const [all, setAll] = useState(false);
  const shown = all ? rows : rows.slice(0, PARTNER_PREVIEW);
  return (
    <div className="table-wrap">
      <table className="data-table" data-testid="partner-table">
        <caption>
          {rows.length} partner countr{rows.length === 1 ? 'y' : 'ies'}; {arcs.length} drawn as arcs
          {unmapped > 0 && ` (${unmapped} without reviewed display geometry, listed only)`}
          {!all && rows.length > PARTNER_PREVIEW && `; largest ${PARTNER_PREVIEW} shown`}
        </caption>
        <thead>
          <tr>
            <th scope="col">Partner</th>
            <th scope="col">Value</th>
            <th scope="col">On map</th>
          </tr>
        </thead>
        <tbody>
          {shown.map((p) => (
            <tr key={p.relationId}>
              <th scope="row">{p.partnerLabel}</th>
              <td>
                {p.value === null ? (
                  <MissingValue reason={p.missingReason ?? 'unavailable'} />
                ) : (
                  <span className="num" data-exact={p.value} title={`exact: ${p.value} US$`}>
                    {displayDecimal(p.value, 'unit.usd').text} US$
                  </span>
                )}
                {p.valuation && <span className="basis"> · {p.valuation}</span>}
                {p.valueSource === 'evidence_as_of' && <span className="field-hint"> · evidence as of cutoff (not in the release)</span>}
              </td>
              <td>{arcs.some((a) => a.id === p.relationId) ? 'arc' : <span className="muted">listed only</span>}</td>
            </tr>
          ))}
        </tbody>
      </table>
      {rows.length > PARTNER_PREVIEW && (
        <button type="button" onClick={() => setAll((a) => !a)} aria-expanded={all}>
          {all ? `Show the largest ${PARTNER_PREVIEW}` : `Show all ${rows.length} partners`}
        </button>
      )}
    </div>
  );
}

function ExposureLegend() {
  const rowsL = legendRows(EXPOSURE_SCALE);
  return (
    <div className="map-legend" aria-label="Map legend">
      <p className="legend-title">
        Exposure, % of baseline GDP (blue = net exporter gains, red = net importer cost; ranges include their lower bound)
      </p>
      <ul className="legend legend-scale">
        {rowsL.map((r) => (
          <li key={r.step}>
            <span className="swatch" style={{ background: r.color }} aria-hidden="true" /> {r.label}
          </li>
        ))}
      </ul>
      <ul className="legend">
        <li>
          <span className="swatch swatch-hatch" aria-hidden="true" /> No complete inputs (hover for the typed reason; not zero)
        </li>
        <li>
          <span className="swatch" style={{ background: NOT_IN_RUN_COLOR }} aria-hidden="true" /> Not in this run
        </li>
        <li>
          <span className="swatch" style={{ background: NO_ENTITY_COLOR, outline: '1px solid #c5cbd3' }} aria-hidden="true" /> Display feature without a
          reviewed entity match
        </li>
        <li>
          <span className="swatch swatch-arc" aria-hidden="true" /> Reported bilateral flow (width ∝ √value)
        </li>
        <li>
          <UncertaintyGlyph kind="unquantified" className="uglyph-inline" /> Values are unquantified (sources publish no uncertainty)
        </li>
      </ul>
      <p className="map-note">
        A simulation: assumption-conditioned stress test, not a forecast and not a GDP loss. Natural Earth boundaries are display-only and not
        authoritative.
      </p>
    </div>
  );
}
