import { useMemo, useState } from 'react';
import { Link, useParams } from 'react-router';
import type { Envelope, SimResult, SimulationStatus } from '../api/types';
import { MODEL_KEYS } from '../api/v2types';
import { BacktestPanel } from '../components/BacktestPanel';
import { ContextBanner } from '../components/ContextBanner';
import { EnvelopeDetails } from '../components/EnvelopeDetails';
import { ExperimentalLabel, IoLabel, ProjectionLabel, SENSITIVITY_LABEL } from '../components/HonestyLabels';
import { IoHeatmap } from '../components/IoHeatmap';
import { JsonView } from '../components/JsonView';
import { PeriodChart } from '../components/PeriodChart';
import { PopulationPyramid } from '../components/PopulationPyramid';
import { ProvenanceButton } from '../components/ProvenanceDrawer';
import { ErrorState, Loading } from '../components/StateViews';
import { UncertaintyGlyph } from '../components/UncertaintyGlyph';
import { ValueView } from '../components/ValueView';
import { displayDecimal, humanize, isRecord, pointInRange, shortId, unitLabel } from '../lib/format';
import {
  IO_CONTRIB,
  basketRows,
  buildSeries,
  countryOf,
  findBacktests,
  ioMatrix,
  isApproximate,
  isMultiPeriod,
  modelKeyOf,
  multiOutputLabel,
  pyramidFor,
  pyramidYears,
  type Series,
} from '../lib/runviews';
import { useSimulation } from '../lib/useSimulation';
import { ResultsView } from './SimulationPage';

const L1_POS: Record<string, string> = {
  upper_end: 'L1 at upper end',
  lower_end: 'L1 at lower end',
  inside: 'L1 inside',
  degenerate: 'no sensitivity',
  outside: 'L1 outside (unexpected)',
};

function explanationOf(st: SimulationStatus) {
  const app = isRecord(st.outcome?.applicability) ? st.outcome!.applicability : {};
  const ex = isRecord(app.explanation) ? app.explanation : {};
  const nc = ex.not_claimed ?? app.not_claimed;
  return {
    notClaimed: Array.isArray(nc) ? nc.map(String) : typeof nc === 'string' ? [nc] : [],
    method: typeof ex.method === 'string' ? ex.method : null,
    label: typeof ex.label === 'string' ? ex.label : typeof app.label === 'string' ? app.label : null,
    assumptions: isRecord(app.assumptions) ? app.assumptions : null,
    grid: Array.isArray(app.sensitivity_grid) ? app.sensitivity_grid : Array.isArray(app.pass_through_grid) ? app.pass_through_grid : null,
  };
}

function SeriesTable({ s }: { s: Series }) {
  const years = [...new Set([...s.l1.map((p) => p.year), ...s.range.map((r) => r.year)])].sort((a, b) => a - b);
  const unit = unitLabel(s.unitRef);
  return (
    <details>
      <summary>Table: {multiOutputLabel(s.outputKey)} ({years.length} periods)</summary>
      <div className="table-wrap">
        <table className="data-table" data-testid="series-table">
          <caption>
            {multiOutputLabel(s.outputKey)} — {s.country}
            {unit ? ` (${unit})` : ''}; periods are half-open [start, end)
          </caption>
          <thead>
            <tr>
              <th scope="col">Period</th>
              <th scope="col">L1 (stated assumptions)</th>
              {s.range.length > 0 && <th scope="col">Sensitivity range (not a confidence interval)</th>}
              <th scope="col">Trace</th>
            </tr>
          </thead>
          <tbody>
            {years.map((y) => {
              const p = s.l1.find((x) => x.year === y);
              const r = s.range.find((x) => x.year === y);
              const pos = p?.value && r ? pointInRange(p.value, r.lower, r.upper) : null;
              return (
                <tr key={y}>
                  <th scope="row">
                    {y} <span className="muted mono">{p ? `[${p.period.start}, ${p.period.end})` : ''}</span>
                  </th>
                  <td>{p ? <ValueView value={p.result.value} /> : <span className="muted">not reported</span>}</td>
                  {s.range.length > 0 && (
                    <td>
                      {r ? (
                        <>
                          <span className="num">
                            {displayDecimal(r.lower, s.unitRef).text} to {displayDecimal(r.upper, s.unitRef).text}
                          </span>
                          {pos && <span className="field-hint"> · {L1_POS[pos]}</span>}
                        </>
                      ) : (
                        <span className="muted">—</span>
                      )}
                    </td>
                  )}
                  <td>{p && <ProvenanceButton kind="sim_result" id={p.result.id} label="Trace" />}</td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </details>
  );
}

function SeriesBlock({ s }: { s: Series }) {
  const approx = isApproximate(s);
  const oneSided = s.range.find((r) => r.oneSidedness)?.oneSidedness;
  return (
    <figure className="series-block" data-output-key={s.outputKey}>
      <figcaption>
        <strong>{multiOutputLabel(s.outputKey)}</strong> <span className="muted">— {s.country}</span>
        {approx && <span className="badge badge-approx">approximate</span>}
        {s.range.length > 0 && (
          <span className="field-hint">
            {' '}
            · ribbon = {SENSITIVITY_LABEL}
            {s.variants.length ? ` over ${s.variants.length} variants` : ''}
          </span>
        )}
      </figcaption>
      <PeriodChart
        l1={s.l1}
        range={s.range}
        unitRef={s.unitRef}
        label={`${multiOutputLabel(s.outputKey)} for ${s.country}, ${s.l1[0]?.year ?? ''}–${s.l1[s.l1.length - 1]?.year ?? ''}${s.range.length ? ', with assumption sensitivity ribbon' : ''}. Table follows.`}
      />
      {oneSided && <p className="field-hint">Range: {oneSided}</p>}
      <SeriesTable s={s} />
    </figure>
  );
}

// ------------------------------------------------------------------ cohort

function CohortView({ st }: { st: SimulationStatus }) {
  const countries = useMemo(() => [...new Set(st.results.map(countryOf))].sort(), [st.results]);
  const [country, setCountry] = useState<string>('');
  const c = country || countries[0] || '';
  const years = useMemo(() => pyramidYears(st.results, c), [st.results, c]);
  const [yearIdx, setYearIdx] = useState(0);
  const year = years[Math.min(yearIdx, years.length - 1)];
  const pyr = useMemo(() => (year !== undefined ? pyramidFor(st.results, c, year) : null), [st.results, c, year]);
  const maxValue = useMemo(() => {
    let m = 0;
    for (const y of years) for (const b of pyramidFor(st.results, c, y)?.bands ?? []) m = Math.max(m, Number(b.female ?? 0), Number(b.male ?? 0));
    return m;
  }, [st.results, c, years]);
  const series = useMemo(() => buildSeries(st.results.filter((r) => countryOf(r) === c), (k) => !/\.age_\d{3}/.test(k)), [st.results, c]);
  const [outKey, setOutKey] = useState('');
  const shown = series.find((s) => s.outputKey === (outKey || 'population.total.all_ages')) ?? series[0];

  return (
    <>
      <section className="panel" aria-labelledby="pyr-h">
        <h2 id="pyr-h">Population pyramid</h2>
        {countries.length > 1 && (
          <label>
            Country
            <select value={c} onChange={(e) => setCountry(e.target.value)}>
              {countries.map((x) => (
                <option key={x}>{x}</option>
              ))}
            </select>
          </label>
        )}
        {years.length === 0 ? (
          <p className="muted" role="status">
            No age-by-sex rows in this run. Request the output <code>population_by_single_age</code> (or the broad age groups by sex) to draw a
            pyramid.
          </p>
        ) : (
          <>
            <div className="year-slider">
              <label htmlFor="pyr-year">Year (1 January)</label>
              <input
                id="pyr-year"
                type="range"
                min={0}
                max={years.length - 1}
                step={1}
                value={Math.min(yearIdx, years.length - 1)}
                onChange={(e) => setYearIdx(Number(e.target.value))}
                aria-valuetext={String(year)}
              />
              <output htmlFor="pyr-year" className="num" data-testid="pyramid-year">
                {year}
              </output>
            </div>
            {pyr && (
              <>
                <PopulationPyramid
                  pyramid={pyr}
                  maxValue={maxValue}
                  label={`Population pyramid of ${c} on 1 January ${year}, ${pyr.kind === 'single_age' ? 'single ages' : 'broad age groups'}, males left and females right. Table follows.`}
                />
                {pyr.kind === 'broad_groups' && <p className="field-hint">Broad disjoint groups only (single ages were not requested for this run).</p>}
                <details>
                  <summary>Table: population by age and sex, {year}</summary>
                  <div className="table-wrap">
                    <table className="data-table">
                      <caption>
                        Population of {c} on 1 January {year} by age and recorded sex (persons)
                      </caption>
                      <thead>
                        <tr>
                          <th scope="col">Age</th>
                          <th scope="col">Males</th>
                          <th scope="col">Females</th>
                        </tr>
                      </thead>
                      <tbody>
                        {pyr.bands.map((b) => (
                          <tr key={b.label}>
                            <th scope="row">{b.label}</th>
                            <td className="num">{b.male === null ? <em>missing</em> : displayDecimal(b.male, 'unit.persons').text}</td>
                            <td className="num">{b.female === null ? <em>missing</em> : displayDecimal(b.female, 'unit.persons').text}</td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                </details>
              </>
            )}
          </>
        )}
      </section>

      <section className="panel" aria-labelledby="ts-h">
        <h2 id="ts-h">Projected indicators</h2>
        {series.length === 0 ? (
          <p className="muted">No dated indicator rows in this run.</p>
        ) : (
          <>
            <label>
              Indicator
              <select value={shown?.outputKey ?? ''} onChange={(e) => setOutKey(e.target.value)}>
                {series.map((s) => (
                  <option key={s.outputKey} value={s.outputKey}>
                    {multiOutputLabel(s.outputKey)}
                  </option>
                ))}
              </select>
            </label>
            {shown && <SeriesBlock s={shown} />}
          </>
        )}
      </section>
    </>
  );
}

// ------------------------------------------------------------------ generic multi-period (retirement age and others)

function MultiPeriodView({ st }: { st: SimulationStatus }) {
  const series = useMemo(() => buildSeries(st.results), [st.results]);
  const countries = useMemo(() => [...new Set(series.map((s) => s.country))], [series]);
  const [country, setCountry] = useState('');
  const c = country || countries[0] || '';
  return (
    <section className="panel" aria-labelledby="mp-h">
      <h2 id="mp-h">Results by year</h2>
      {countries.length > 1 && (
        <label>
          Country
          <select value={c} onChange={(e) => setCountry(e.target.value)}>
            {countries.map((x) => (
              <option key={x}>{x}</option>
            ))}
          </select>
        </label>
      )}
      {series.length === 0 && <p className="muted">No dated rows in this run.</p>}
      <div className="series-grid">
        {series
          .filter((s) => s.country === c)
          .map((s) => (
            <SeriesBlock key={s.outputKey} s={s} />
          ))}
      </div>
    </section>
  );
}

// ------------------------------------------------------------------ IO price

function pct(r: SimResult | null | undefined) {
  if (!r) return <span className="muted">not reported</span>;
  return <ValueView value={r.value} />;
}

function IoView({ st }: { st: SimulationStatus }) {
  const matrix = useMemo(() => ioMatrix(st.results), [st.results]);
  const baskets = useMemo(() => basketRows(st.results), [st.results]);
  const labels = useMemo(() => {
    const m = new Map<string, string>();
    for (const r of st.results) if (r.subject) m.set(countryOf(r), r.subject.label);
    return m;
  }, [st.results]);
  const label = (iso: string) => (labels.get(iso) && labels.get(iso) !== iso ? `${labels.get(iso)} (${iso})` : iso);
  const isL2 = st.run?.fidelity === 'L2';
  const oneSided = [
    ...new Set(
      baskets
        .map((b) => (b.range ? (b.range.value as unknown as { one_sidedness?: unknown }).one_sidedness : null))
        .filter((x): x is string => typeof x === 'string'),
    ),
  ];
  const shocks = Array.isArray((st.scenario?.spec?.intervention as Record<string, unknown> | undefined)?.shocks)
    ? ((st.scenario!.spec.intervention as Record<string, unknown>).shocks as Record<string, unknown>[])
    : [];
  return (
    <>
      {shocks.length > 0 && (
        <section className="panel" aria-labelledby="shock-h">
          <h2 id="shock-h">Stated shock</h2>
          <ul>
            {shocks.map((s, i) => (
              <li key={i}>
                {String(s.origin)} · industry <span className="mono">{String(s.industry)}</span>: output price {Number(s.price_change_percent) > 0 ? '+' : ''}
                {String(s.price_change_percent)} % (exogenous)
              </li>
            ))}
          </ul>
          <p className="field-hint">An industry of the table is shocked as a whole (e.g. NACE B mining and quarrying is broader than crude petroleum).</p>
        </section>
      )}

      <section className="panel" aria-labelledby="basket-h">
        <h2 id="basket-h">Household basket price change by country</h2>
        {baskets.length === 0 ? (
          <p className="muted">No basket rows in this run.</p>
        ) : (
          <div className="table-wrap">
            <table className="data-table" data-testid="basket-table">
              <caption>
                Household basket at basic prices, % change (L1 full pass-through){isL2 ? `; ${SENSITIVITY_LABEL} over the pass-through grid` : ''};
                decomposition by product origin and by shock origin (each pair sums to the basket change before rounding)
              </caption>
              <thead>
                <tr>
                  <th scope="col">Country</th>
                  <th scope="col">Basket (L1)</th>
                  {isL2 && <th scope="col">Sensitivity range</th>}
                  <th scope="col">Domestic products</th>
                  <th scope="col">Imported products</th>
                  <th scope="col">Domestic shocks</th>
                  <th scope="col">Foreign shocks</th>
                </tr>
              </thead>
              <tbody>
                {baskets.map((b) => (
                  <tr key={b.country}>
                    <th scope="row">{label(b.country)}</th>
                    <td>{pct(b.basket)}</td>
                    {isL2 && (
                      <td>
                        {b.range ? <ValueView value={b.range.value} /> : <span className="muted">—</span>}
                      </td>
                    )}
                    {(Object.keys(IO_CONTRIB) as (keyof typeof IO_CONTRIB)[]).map((k) => (
                      <td key={k}>{pct(b.contributions[k])}</td>
                    ))}
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>

      {oneSided.length > 0 && (
        <p className="field-hint" data-testid="one-sidedness">
          Sensitivity range: {oneSided.join(' / ')}
        </p>
      )}
      <section className="panel" aria-labelledby="hm-h">
        <h2 id="hm-h">Output-price change by country and industry</h2>
        {matrix.countries.length === 0 ? <p className="muted">No industry rows in this run.</p> : <IoHeatmap matrix={matrix} countryLabel={label} />}
      </section>
    </>
  );
}

// ------------------------------------------------------------------ page

/** The API caps result rows per response: say so wherever results are shown (never imply completeness). */
export function TruncationNotice({ env }: { env: { warnings?: { code: string; message: string }[] } | null | undefined }) {
  const w = env?.warnings?.find((x) => x.code === 'results_truncated');
  if (!w) return null;
  return (
    <p className="bound-flag" role="status" data-testid="results-truncated">
      <strong>Incomplete listing:</strong> {w.message} Charts, tables and the heatmap below show only the rows returned; missing rows are not
      zero and not absent from the run.
    </p>
  );
}

export function RunBody({ id, env }: { id: string; env: Envelope<SimulationStatus> }) {
  const st = env.data;
  const model = modelKeyOf(st);
  const ex = explanationOf(st);
  const multi = isMultiPeriod(st.results);
  const backtests = findBacktests(st);
  const experimental = model !== MODEL_KEYS.crude && model !== null;
  const reviewStatus = (env.model_versions ?? [])[0]?.approval_status ?? null;
  const app = isRecord(st.outcome?.applicability) ? st.outcome!.applicability : {};
  const countryStates = isRecord(app.countries) ? Object.entries(app.countries) : [];
  const skipped = Array.isArray(app.skipped_countries) ? app.skipped_countries : [];

  if (model === MODEL_KEYS.crude) {
    return (
      <>
        <TruncationNotice env={env} />
        <p>
          <Link to={`/map?sim=${encodeURIComponent(id)}`}>Show this run on the world map</Link>
        </p>
        <ResultsView simId={id} env={env} />
      </>
    );
  }

  return (
    <>
      {(model === MODEL_KEYS.cohort || model === MODEL_KEYS.retirement || (multi && model !== MODEL_KEYS.io)) && <ProjectionLabel />}
      {model === MODEL_KEYS.io && <IoLabel l2={st.run?.fidelity === 'L2'} />}
      {experimental && <ExperimentalLabel reviewStatus={reviewStatus} />}
      <TruncationNotice env={env} />

      <section className="panel" aria-labelledby="runsum-h">
        <h2 id="runsum-h">Run</h2>
        <p>
          Simulation <span className="mono">{shortId(id)}</span> · model <span className="mono">{model ?? 'not stated'}</span> · fidelity{' '}
          {st.run?.fidelity ?? '—'} · outcome <strong>{st.outcome ? humanize(st.outcome.status) : 'not finished'}</strong> · {st.results.length} result
          rows
        </p>
        {ex.label && <p className="muted">{ex.label}</p>}
        {st.outcome && st.outcome.status !== 'succeeded' && (
          <div className="problem" role="alert">
            <p>
              <strong>No outputs reported:</strong> run ended as {humanize(st.outcome.status)}.
            </p>
            <JsonView value={st.outcome.problems} />
          </div>
        )}
        {countryStates.some(([, v]) => isRecord(v) && v.status !== 'ready') || skipped.length > 0 ? (
          <div className="table-wrap">
            <table className="data-table">
              <caption>Countries without results (typed reasons; never zero-filled)</caption>
              <thead>
                <tr>
                  <th scope="col">Country</th>
                  <th scope="col">Status</th>
                  <th scope="col">Problems</th>
                </tr>
              </thead>
              <tbody>
                {countryStates
                  .filter(([, v]) => isRecord(v) && v.status !== 'ready')
                  .map(([iso, v]) => (
                    <tr key={iso}>
                      <th scope="row">{iso}</th>
                      <td>
                        <UncertaintyGlyph kind="missing" className="uglyph-inline" /> {humanize(String((v as Record<string, unknown>).status))}
                      </td>
                      <td>
                        <JsonView value={(v as Record<string, unknown>).problems} />
                      </td>
                    </tr>
                  ))}
                {skipped.map((s, i) => (
                  <tr key={`sk${i}`}>
                    <th scope="row">{typeof s === 'string' ? s : String((s as Record<string, unknown>)?.country ?? '—')}</th>
                    <td>skipped (missing inputs)</td>
                    <td>{typeof s === 'string' ? '' : <JsonView value={s} />}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : null}
      </section>

      {model === MODEL_KEYS.cohort && <CohortView st={st} />}
      {model === MODEL_KEYS.io && <IoView st={st} />}
      {model !== MODEL_KEYS.cohort && model !== MODEL_KEYS.io && multi && <MultiPeriodView st={st} />}
      {model !== MODEL_KEYS.cohort && model !== MODEL_KEYS.io && !multi && st.results.length > 0 && (
        <section className="panel" aria-labelledby="gen-h">
          <h2 id="gen-h">Results</h2>
          <div className="table-wrap">
            <table className="data-table">
              <caption>Result rows ({st.results.length})</caption>
              <thead>
                <tr>
                  <th scope="col">Country</th>
                  <th scope="col">Output</th>
                  <th scope="col">Variant</th>
                  <th scope="col">Value</th>
                </tr>
              </thead>
              <tbody>
                {st.results.slice(0, 500).map((r) => (
                  <tr key={r.id}>
                    <th scope="row">{countryOf(r)}</th>
                    <td className="mono">{r.output_key}</td>
                    <td className="mono">{r.variant}</td>
                    <td>
                      <ValueView value={r.value} />
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </section>
      )}

      {(model === MODEL_KEYS.cohort || model === MODEL_KEYS.retirement || backtests.length > 0) && (
        <BacktestPanel
          backtests={backtests}
          source={model === MODEL_KEYS.cohort ? 'the method back-test in docs/dev/v2_pop_backtest.md' : undefined}
          unitRef={model === MODEL_KEYS.cohort ? 'unit.persons' : undefined}
        />
      )}

      <aside className="panel limits-panel" aria-labelledby="runlim-h" data-testid="result-limits">
        <h2 id="runlim-h">Assumptions and limits</h2>
        {ex.method && (
          <p>
            <strong>Method:</strong> {ex.method}
          </p>
        )}
        {ex.notClaimed.length > 0 && (
          <p>
            <strong>Not claimed:</strong> {ex.notClaimed.join('; ')}.
          </p>
        )}
        {(env.assumptions ?? []).length > 0 && (
          <ul>
            {env.assumptions!.map((a, i) => (
              <li key={i}>{a}</li>
            ))}
          </ul>
        )}
        {ex.assumptions && <JsonView value={ex.assumptions} />}
        {ex.grid && ex.grid.length > 0 && (
          <details>
            <summary>Sensitivity grid ({ex.grid.length} variants; scenario assumptions, no probability)</summary>
            <JsonView value={ex.grid} />
          </details>
        )}
      </aside>
      <EnvelopeDetails meta={env} />
    </>
  );
}

export default function RunPage() {
  const { id = '' } = useParams();
  const { job, env, error, done } = useSimulation(id);
  const st = env?.data;
  return (
    <div className="simulation-screen">
      <h1>Run {shortId(id)}</h1>
      <ContextBanner
        meta={env ?? { context_mode: 'simulation', modality: 'assumption_conditioned_scenario', origin: 'none' }}
        simulation
        scope={st ? `Results of ${modelKeyOf(st) ?? 'a model'} (${st.run?.fidelity ?? '—'})` : 'Run status'}
      />
      {!done && !error && (
        <p role="status" aria-live="polite">
          Job <span className="mono">{shortId(id)}</span>: {job ? humanize(job.status) : 'loading'}…
        </p>
      )}
      {!done && !error && <Loading what="run" />}
      {!!error && <ErrorState error={error} what="The run" />}
      {job?.last_error && (
        <div className="problem" role="alert">
          <p>
            <strong>Job error</strong>
          </p>
          <JsonView value={job.last_error} />
        </div>
      )}
      {env && <RunBody id={id} env={env} />}
    </div>
  );
}
