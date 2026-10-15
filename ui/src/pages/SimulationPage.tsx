import { useCallback, useEffect, useMemo, useRef, useState, type FormEvent } from 'react';
import { useSearchParams } from 'react-router';
import {
  cancelJob,
  createScenario,
  getBeliefReleases,
  getBeliefs,
  getJob,
  getSimulation,
  newIdempotencyKey,
  startSimulation,
} from '../api/client';
import { ApiError } from '../api/problem';
import {
  COUNTRY_CODES,
  TERMINAL_JOB_STATUSES,
  type BaselineReleaseRef,
  type CountryCode,
  type Envelope,
  type Job,
  type SimResult,
  type SimulationStatus,
} from '../api/types';
import { ContextBanner, SIMULATION_BANNER_TEXT } from '../components/ContextBanner';
import { EnvelopeDetails } from '../components/EnvelopeDetails';
import { JsonView } from '../components/JsonView';
import { ProvenanceButton } from '../components/ProvenanceDrawer';
import { ErrorState, Loading, ProblemView } from '../components/StateViews';
import { UncertaintyPanel } from '../components/UncertaintyPanel';
import { ValueView } from '../components/ValueView';
import { formatDateTime, formatDecimalString, humanize, isRecord, missingReasonLabel, shortId, sourceFlagNotes, unitLabel } from '../lib/format';
import {
  ALL_OUTPUTS,
  DEFAULT_FORM,
  EXPORT_ELASTICITY_RANGE,
  IMPORT_ELASTICITY_RANGE,
  buildScenarioSpec,
  exportContext,
  outputLabel,
  resultsToCsv,
  resultsToJson,
  validateBaselineReleaseId,
  validateForm,
  type ScenarioForm,
} from '../lib/scenario';
import { useApi } from '../lib/useApi';

const LIMITS_TEXT = [
  'One annual accounting period of border-value exposure under the stated price multipliers.',
  'Not a GDP loss, welfare change, consumer inflation, fiscal revenue, unemployment or balance-of-payments forecast.',
  'No general-equilibrium closure; quantities are fixed (L1) or follow assumed elasticities (L2).',
  'Imports are usually CIF-valued and exports FOB; the multiplier applies to the reported border value.',
  'A headline Brent price change is not automatically this intervention.',
  'Countries missing a required input are rejected (missing_input), never imputed as zero.',
];

function download(name: string, content: string, type: string) {
  const blob = new Blob([content], { type });
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = name;
  document.body.appendChild(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

function jobProgress(job: Job | null): { label: string; fraction: number | null } {
  if (!job) return { label: 'not started', fraction: null };
  const p = job.progress ?? {};
  const done = typeof p.done === 'number' ? p.done : null;
  const total = typeof p.total === 'number' ? p.total : null;
  const fraction = typeof p.fraction === 'number' ? p.fraction : done !== null && total ? done / total : null;
  const stage = typeof p.stage === 'string' ? p.stage : typeof p.message === 'string' ? p.message : null;
  return { label: `${humanize(job.status)}${stage ? ` — ${stage}` : ''}`, fraction };
}

/** Variant of the separate L2 row whose value is the sensitivity range over the grid (kind sensitivity_range). */
export const SENSITIVITY_VARIANT = 'L2:sensitivity_range';

/** Group results by country and output; L1 first, the L2 sensitivity-range row, then L2 grid variants. */
function groupResults(results: SimResult[]) {
  const by = new Map<string, { country: string; output: string; l1?: SimResult; range?: SimResult; variants: SimResult[] }>();
  for (const r of results) {
    const country = r.subject?.label ?? String(r.applicability?.country ?? '—');
    const key = `${country}|${r.output_key}`;
    const g = by.get(key) ?? { country, output: r.output_key, variants: [] };
    if (r.variant === 'L1') g.l1 = r;
    else if (r.variant === SENSITIVITY_VARIANT) g.range = r;
    else g.variants.push(r);
    by.set(key, g);
  }
  return [...by.values()].sort(
    (a, b) =>
      a.country.localeCompare(b.country) ||
      ALL_OUTPUTS.findIndex((o) => o.key === a.output) - ALL_OUTPUTS.findIndex((o) => o.key === b.output),
  );
}

interface BaselineInput {
  value?: string | null;
  valuation?: string | null;
  reference_year?: number | null;
  concept?: string;
  estimate_id?: string | null;
  /** Provider source-flag notes for this input (e.g. provider estimate, not directly reported). */
  flags?: string[];
}

/** Baseline inputs (M, X, G) per country, from result applicability (pinned release values). */
function baselineInputs(results: SimResult[]): Map<string, Record<string, BaselineInput>> {
  const out = new Map<string, Record<string, BaselineInput>>();
  for (const r of results) {
    const country = r.subject?.label ?? String(r.applicability?.country ?? '—');
    if (out.has(country)) continue;
    const inputs = r.applicability?.inputs;
    if (!isRecord(inputs)) continue;
    const flags = isRecord(r.applicability?.source_flags) ? (r.applicability.source_flags as Record<string, unknown>) : {};
    const withFlags: Record<string, BaselineInput> = {};
    for (const [k, v] of Object.entries(inputs as Record<string, BaselineInput>)) {
      const f = flags[k];
      withFlags[k] = { ...v, flags: isRecord(f) ? sourceFlagNotes(f) : [] };
    }
    out.set(country, withFlags);
  }
  return out;
}

/** The model's own statement of what the result is not, its uncertainty and sign convention. */
function explanationOf(outcome: SimulationStatus['outcome'], results: SimResult[]) {
  const app = outcome && isRecord(outcome.applicability) ? outcome.applicability : {};
  const ex = isRecord(app.explanation) ? app.explanation : {};
  const str = (v: unknown) => (typeof v === 'string' && v.trim() ? v : null);
  const fromResult = results.map((r) => str(r.applicability?.not_claimed)).find((x) => x) ?? null;
  return {
    notClaimed: str(app.not_claimed) ?? str(ex.not_claimed) ?? fromResult,
    uncertainty: str(ex.uncertainty),
    signConvention: str(ex.sign_convention),
  };
}

const INPUT_LABELS: Record<string, string> = {
  M: 'Crude imports (M)',
  X: 'Crude exports (X)',
  G: 'Nominal GDP (G)',
};

function BaselineCell({ input }: { input: BaselineInput | undefined }) {
  if (!input) return <span className="muted">not reported</span>;
  if (input.value === null || input.value === undefined) return <span className="missing-value">missing (not in baseline)</span>;
  return (
    <span>
      <span className="num">{formatDecimalString(input.value)}</span> <span className="unit">US$</span>
      {input.valuation && <span className="basis"> · {input.valuation}</span>}
      {input.estimate_id && (
        <>
          {' '}
          <ProvenanceButton kind="estimate" id={input.estimate_id} label="Trace" />
        </>
      )}
      {input.flags && input.flags.length > 0 && <span className="source-flag field-hint"> {input.flags.join('; ')}</span>}
    </span>
  );
}

interface MissingInputRow {
  country?: string;
  input?: string;
  concept?: string;
  year?: number | string;
  state?: string;
}

/** A missing_input rejection listed as data gaps (never zero), above the raw problem details. */
function MissingInputs({ error }: { error: unknown }) {
  if (!(error instanceof ApiError) || error.code !== 'missing_input') return null;
  const details = error.problem.details;
  const raw = isRecord(details) && Array.isArray(details.missing) ? details.missing : Array.isArray(error.problem.missing) ? error.problem.missing : [];
  const rows = (raw as unknown[]).filter(isRecord) as MissingInputRow[];
  if (rows.length === 0) return null;
  const countries = [...new Set(rows.map((r) => r.country).filter(Boolean))];
  return (
    <div className="table-wrap" data-testid="missing-inputs">
      <table className="data-table">
        <caption>
          Run rejected: required inputs missing for {countries.join(', ')} — a data gap, not zero. No country was dropped and no
          value was imputed.
        </caption>
        <thead>
          <tr>
            <th scope="col">Country</th>
            <th scope="col">Input</th>
            <th scope="col">Concept</th>
            <th scope="col">Year</th>
            <th scope="col">State in pinned release</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((r, i) => (
            <tr key={`${r.country}-${r.input}-${i}`}>
              <th scope="row">{r.country ?? '—'}</th>
              <td>{r.input ? (INPUT_LABELS[r.input] ?? r.input) : '—'}</td>
              <td className="mono">{r.concept ?? '—'}</td>
              <td>{r.year ?? '—'}</td>
              <td>
                <span className="missing-value">
                  {r.state?.startsWith('missing:') ? `missing (${missingReasonLabel(r.state.slice(8))})` : (r.state ?? 'missing')}
                </span>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

export function ResultsView({ simId, env }: { simId: string; env: Envelope<SimulationStatus> }) {
  const st = env.data;
  const groups = useMemo(() => groupResults(st.results), [st.results]);
  const inputs = useMemo(() => baselineInputs(st.results), [st.results]);
  const isL2 = st.run?.fidelity === 'L2';
  const variantNames = useMemo(
    () => [...new Set(st.results.filter((r) => r.variant !== 'L1' && r.variant !== SENSITIVITY_VARIANT).map((r) => r.variant))].sort(),
    [st.results],
  );
  const modelVersions = (env.model_versions ?? []).map((m) => `${m.model_key}@${m.version}`);
  const ctx = exportContext(simId, st, modelVersions, env.assumptions ?? []);
  const outcome = st.outcome;
  const explanation = useMemo(() => explanationOf(outcome, st.results), [outcome, st.results]);
  const notClaimed = explanation.notClaimed;

  return (
    <section className="panel" aria-labelledby="results-h">
      <h2 id="results-h">Results</h2>
      <p>
        Simulation <span className="mono">{shortId(simId)}</span> · scenario{' '}
        <span className="mono" title={st.scenario?.id}>
          {shortId(st.scenario?.id)}
        </span>{' '}
        · fidelity {st.run?.fidelity ?? '—'} · outcome <strong>{outcome ? humanize(outcome.status) : 'not finished'}</strong>
        {outcome?.cache_hit_of && <span className="muted"> (reused identical run {shortId(outcome.cache_hit_of)})</span>}
      </p>
      {outcome && outcome.status !== 'succeeded' && (
        <div className="problem" role="alert">
          <p>
            <strong>No outputs reported:</strong> run ended as {humanize(outcome.status)}.
          </p>
          <JsonView value={outcome.problems} />
        </div>
      )}

      {inputs.size > 0 && (
        <div className="table-wrap">
          <table className="data-table">
            <caption>Baseline inputs from pinned release (reference year {String(st.scenario?.spec?.reference_year ?? '—')})</caption>
            <thead>
              <tr>
                <th scope="col">Country</th>
                {Object.keys(INPUT_LABELS).map((k) => (
                  <th scope="col" key={k}>
                    {INPUT_LABELS[k]}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {[...inputs.entries()].map(([country, inp]) => (
                <tr key={country}>
                  <th scope="row">{country}</th>
                  {Object.keys(INPUT_LABELS).map((k) => (
                    <td key={k}>
                      <BaselineCell input={inp[k]} />
                    </td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {groups.length > 0 && (
        <div className="table-wrap">
          <table className="data-table" data-testid="comparison-table">
            <caption>
              Scenario outputs relative to baseline (SIMULATION; assumption-conditioned)
              {isL2 && ' — L2 column shows the sensitivity range (not a confidence interval)'}
            </caption>
            <thead>
              <tr>
                <th scope="col">Country</th>
                <th scope="col">Output</th>
                <th scope="col">Fixed quantities (L1)</th>
                {isL2 && <th scope="col">Sensitivity range (not a confidence interval)</th>}
                <th scope="col">Uncertainty</th>
              </tr>
            </thead>
            <tbody>
              {groups.map((g) => (
                <tr key={`${g.country}-${g.output}`}>
                  <th scope="row">{g.country}</th>
                  <td>{outputLabel(g.output)}</td>
                  <td>
                    <ValueView value={g.l1?.value} />
                  </td>
                  {isL2 && (
                    <td>
                      {g.range && g.range.value.kind === 'sensitivity_range' ? (
                        <ValueView value={g.range.value} />
                      ) : g.l1?.uncertainty?.kind === 'sensitivity_range' && g.l1.uncertainty.lower && g.l1.uncertainty.upper ? (
                        <span className="sensitivity-range">
                          {formatDecimalString(g.l1.uncertainty.lower)} to {formatDecimalString(g.l1.uncertainty.upper)}{' '}
                          {unitLabel(g.l1.value.kind === 'decimal' ? g.l1.value.unit_ref : undefined)}
                        </span>
                      ) : (
                        <span className="muted">not reported</span>
                      )}
                    </td>
                  )}
                  <td>
                    <UncertaintyPanel uncertainty={g.l1?.uncertainty} compact />
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {isL2 && variantNames.length > 0 && (
        <details>
          <summary>L2 grid variants ({variantNames.length} assumed elasticity pairs)</summary>
          <div className="table-wrap">
            <table className="data-table">
              <caption>Outputs at each assumed elasticity pair (scenario assumptions; no probability attached)</caption>
              <thead>
                <tr>
                  <th scope="col">Country</th>
                  <th scope="col">Output</th>
                  {variantNames.map((v) => (
                    <th scope="col" key={v} className="mono">
                      {v.replace(/^L2:/, '')}
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {groups.map((g) => (
                  <tr key={`${g.country}-${g.output}-v`}>
                    <th scope="row">{g.country}</th>
                    <td>{outputLabel(g.output)}</td>
                    {variantNames.map((v) => {
                      const r = g.variants.find((x) => x.variant === v) ?? (v === 'L2:eM=0;eX=0' ? g.l1 : undefined);
                      return <td key={v}>{r ? <ValueView value={r.value} /> : <span className="muted">same as L1</span>}</td>;
                    })}
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </details>
      )}

      {(notClaimed || st.results.length > 0) && (
        <aside className="panel limits-panel" aria-label="Limits of this result" data-testid="result-limits">
          <h3>Limits</h3>
          <p>{notClaimed ?? LIMITS_TEXT[1]}</p>
          {explanation.uncertainty && (
            <p>
              <strong>Uncertainty:</strong> {explanation.uncertainty}
            </p>
          )}
          {explanation.signConvention && (
            <p>
              <strong>Sign convention:</strong> {explanation.signConvention}
            </p>
          )}
        </aside>
      )}

      {st.results.length > 0 && (
        <div className="export-actions">
          <button
            type="button"
            onClick={() => download(`reva-simulation-${simId}.csv`, resultsToCsv(ctx, st.results), 'text/csv;charset=utf-8')}
          >
            Export CSV (with scenario context)
          </button>{' '}
          <button
            type="button"
            onClick={() => download(`reva-simulation-${simId}.json`, resultsToJson(ctx, st.results), 'application/json')}
          >
            Export JSON (with scenario context)
          </button>
          <p className="field-hint">Exports carry the SIMULATION label, scenario id, baseline release, cutoff, model version and assumptions.</p>
        </div>
      )}
      <EnvelopeDetails meta={env} />
    </section>
  );
}

export default function SimulationPage() {
  const [params, setParams] = useSearchParams();
  const simId = params.get('sim');

  const beliefs = useApi((s) => getBeliefs({ limit: 1 }, s), []);
  const releases = useApi((s) => getBeliefReleases({ limit: 20 }, s), []);
  const [chosenRelease, setChosenRelease] = useState<string>('');
  const [form, setForm] = useState<ScenarioForm>(DEFAULT_FORM);
  const [touched, setTouched] = useState(false);
  const [submitError, setSubmitError] = useState<unknown>(null);
  const [busy, setBusy] = useState<null | 'creating' | 'starting'>(null);
  const [job, setJob] = useState<Job | null>(null);
  const [jobError, setJobError] = useState<unknown>(null);
  const [cancelState, setCancelState] = useState<string | null>(null);
  const [simEnv, setSimEnv] = useState<Envelope<SimulationStatus> | null>(null);
  const keys = useRef<{ spec: string; scenario: string; simulation: string } | null>(null);

  const releaseOptions: BaselineReleaseRef[] = useMemo(() => {
    const list: BaselineReleaseRef[] = (releases.data?.data ?? []).map((r) => ({
      id: r.id,
      sequence: r.sequence,
      knowledge_cutoff: r.knowledge_cutoff,
      published_at: r.published_at,
      origin: r.origin,
      method: r.method,
    }));
    const latest = beliefs.data?.baseline_release;
    if (latest && !list.some((r) => r.id === latest.id)) list.unshift(latest);
    return list;
  }, [releases.data, beliefs.data]);

  const release = releaseOptions.find((r) => r.id === (chosenRelease || beliefs.data?.baseline_release?.id)) ?? null;
  const releaseError = beliefs.status === 'loading' && !release ? null : validateBaselineReleaseId(release?.id);
  const errors = validateForm(form);
  const formValid = Object.keys(errors).length === 0;
  const running = !!job && !(TERMINAL_JOB_STATUSES as readonly string[]).includes(job.status);
  const canSubmit = formValid && !releaseError && !!release && !busy && !running;

  // Poll the job until it reaches a terminal state, then load the simulation record.
  useEffect(() => {
    if (!simId) return;
    let stopped = false;
    let delay = 800;
    let timer: ReturnType<typeof setTimeout> | undefined;
    const tick = async () => {
      try {
        const j = await getJob(simId);
        if (stopped) return;
        setJob(j);
        setJobError(null);
        if ((TERMINAL_JOB_STATUSES as readonly string[]).includes(j.status)) {
          const env = await getSimulation(simId);
          if (!stopped) setSimEnv(env);
          return;
        }
      } catch (e) {
        if (stopped) return;
        setJobError(e);
        if (e instanceof ApiError && (e.kind === 'not_found' || e.kind === 'forbidden')) return;
      }
      delay = Math.min(delay * 1.5, 4000);
      timer = setTimeout(tick, delay);
    };
    setSimEnv(null);
    void tick();
    return () => {
      stopped = true;
      if (timer) clearTimeout(timer);
    };
  }, [simId]);

  const submit = useCallback(
    async (e: FormEvent) => {
      e.preventDefault();
      setTouched(true);
      setSubmitError(null);
      if (!canSubmit || !release) return;
      let spec;
      try {
        spec = buildScenarioSpec(form, release);
      } catch (err) {
        setSubmitError(err);
        return;
      }
      // Reuse idempotency keys when retrying the identical request; new keys for a changed spec.
      const specJson = JSON.stringify(spec);
      if (!keys.current || keys.current.spec !== specJson) {
        keys.current = { spec: specJson, scenario: newIdempotencyKey(), simulation: newIdempotencyKey() };
      }
      try {
        setBusy('creating');
        const created = await createScenario(spec, keys.current.scenario);
        setBusy('starting');
        const accepted = await startSimulation({ scenario_id: created.scenario_id, fidelity: spec.fidelity }, keys.current.simulation);
        setJob(null);
        setCancelState(null);
        setParams({ sim: accepted.job_id });
      } catch (err) {
        setSubmitError(err);
      } finally {
        setBusy(null);
      }
    },
    [canSubmit, release, form, setParams],
  );

  const cancel = async () => {
    if (!simId) return;
    try {
      const r = await cancelJob(simId);
      setCancelState(`Cancellation requested (${humanize(r.status)}).`);
    } catch (err) {
      setCancelState(err instanceof ApiError ? (err.problem.detail ?? err.message) : String(err));
    }
  };

  const setGrid = (i: number, field: 'importElasticity' | 'exportElasticity', v: string) =>
    setForm((f) => ({ ...f, grid: f.grid.map((g, j) => (j === i ? { ...g, [field]: v } : g)) }));

  const progress = jobProgress(job);
  // The scenario's reference year is its valid/reference time (one annual accounting period).
  const runYear = simEnv?.data.scenario?.spec?.reference_year;
  const referenceTime = simEnv?.valid_time
    ? undefined
    : `${simEnv ? String(runYear ?? 'not stated') : form.referenceYear} (scenario reference year; one annual accounting period)`;
  const showErr = (k: string) => (touched && errors[k] ? <span className="field-error">{errors[k]}</span> : null);

  return (
    <div className="simulation-screen">
      <div className="simulation-screen-banner" role="region" aria-label="Simulation context" data-testid="simulation-screen-banner">
        {SIMULATION_BANNER_TEXT}
      </div>
      <h1>Simulation: crude border-price exposure</h1>
      <ContextBanner
        meta={
          simEnv ?? {
            context_mode: 'simulation',
            modality: 'assumption_conditioned_scenario',
            origin: release?.origin ?? 'none',
            baseline_release: release,
            knowledge_cutoff: release?.knowledge_cutoff ?? null,
            warnings: beliefs.data?.warnings ?? [],
          }
        }
        simulation
        referenceTime={referenceTime}
        scope={simId ? 'Scenario run' : 'Scenario definition (no run yet)'}
      />

      <section className="panel" aria-labelledby="baseline-h">
        <h2 id="baseline-h">Baseline release</h2>
        {beliefs.status === 'loading' && <Loading what="published baseline release" />}
        {beliefs.status === 'error' && <ErrorState error={beliefs.error} what="The baseline release" onRetry={beliefs.reload} />}
        {releaseOptions.length > 0 && (
          <label>
            Pinned belief release
            <select value={release?.id ?? ''} onChange={(e) => setChosenRelease(e.target.value)}>
              {releaseOptions.map((r) => (
                <option key={r.id} value={r.id}>
                  #{r.sequence ?? '—'} · cutoff {formatDateTime(r.knowledge_cutoff)} · {r.origin === 'test_fixture' ? 'TEST FIXTURE' : r.origin}
                  {' · '}
                  {shortId(r.id)}
                </option>
              ))}
            </select>
          </label>
        )}
        {release && (
          <p className="muted">
            Release <span className="mono">{release.id}</span>, knowledge cutoff {formatDateTime(release.knowledge_cutoff)}. The scenario
            is pinned to this release and cutoff.
          </p>
        )}
        {releaseError && (
          <p role="alert" className="field-error" data-testid="baseline-error">
            {releaseError}
          </p>
        )}
      </section>

      <form className="panel" onSubmit={submit} aria-labelledby="assumptions-h" noValidate>
        <h2 id="assumptions-h">Assumptions</h2>
        <div className="form-grid">
          <label>
            Title
            <input type="text" value={form.title} maxLength={200} onChange={(e) => setForm({ ...form, title: e.target.value })} />
            {showErr('title')}
          </label>
          <fieldset>
            <legend>Countries</legend>
            <div className="inline-checks">
              {COUNTRY_CODES.map((c: CountryCode) => (
                <label key={c}>
                  <input
                    type="checkbox"
                    checked={form.countries.includes(c)}
                    onChange={(e) =>
                      setForm({
                        ...form,
                        countries: e.target.checked ? [...form.countries, c] : form.countries.filter((x) => x !== c),
                      })
                    }
                  />
                  {c}
                </label>
              ))}
            </div>
            {form.countries.includes('DZA') && (
              <p className="field-hint" role="note">
                Algeria: no eligible crude trade inputs are known for recent years. The API is expected to reject the run with
                missing_input; Algeria is never dropped silently or treated as zero.
              </p>
            )}
            {showErr('countries')}
          </fieldset>
          <label>
            Reference year
            <select value={form.referenceYear} onChange={(e) => setForm({ ...form, referenceYear: e.target.value })}>
              {['2020', '2021', '2022', '2023', '2024'].map((y) => (
                <option key={y}>{y}</option>
              ))}
            </select>
            {showErr('referenceYear')}
          </label>
          <label>
            Import price multiplier (ratio)
            <input type="text" inputMode="decimal" value={form.importMultiplier} onChange={(e) => setForm({ ...form, importMultiplier: e.target.value })} />
            <span className="field-hint">1.25 = +25% on the reported border value (0 &lt; p ≤ 3)</span>
            {showErr('importMultiplier')}
          </label>
          <label>
            Export price multiplier (ratio)
            <input type="text" inputMode="decimal" value={form.exportMultiplier} onChange={(e) => setForm({ ...form, exportMultiplier: e.target.value })} />
            {showErr('exportMultiplier')}
          </label>
          <fieldset>
            <legend>Fidelity</legend>
            <div className="inline-checks">
              <label>
                <input type="radio" name="fidelity" checked={form.fidelity === 'L1'} onChange={() => setForm({ ...form, fidelity: 'L1' })} />
                L1 — fixed quantities
              </label>
              <label>
                <input type="radio" name="fidelity" checked={form.fidelity === 'L2'} onChange={() => setForm({ ...form, fidelity: 'L2' })} />
                L2 — elasticity sensitivity
              </label>
            </div>
          </fieldset>
          <fieldset>
            <legend>Outputs</legend>
            {ALL_OUTPUTS.map((o) => (
              <label key={o.key}>
                <input
                  type="checkbox"
                  checked={form.outputs.includes(o.key)}
                  onChange={(e) =>
                    setForm({ ...form, outputs: e.target.checked ? [...form.outputs, o.key] : form.outputs.filter((x) => x !== o.key) })
                  }
                />{' '}
                {o.label}
              </label>
            ))}
            {showErr('outputs')}
          </fieldset>
          <label>
            Seed
            <input type="text" inputMode="numeric" value={form.seed} onChange={(e) => setForm({ ...form, seed: e.target.value })} />
            {showErr('seed')}
          </label>
        </div>

        {form.fidelity === 'L2' && (
          <fieldset>
            <legend>Elasticity grid (scenario assumptions)</legend>
            <p className="field-hint">
              Each pair is an assumed quantity elasticity. The spread of outputs across the grid is a sensitivity range, not a
              confidence interval. ε = 0 reproduces L1 exactly. Import ε ∈ [{IMPORT_ELASTICITY_RANGE.join(', ')}], export ε ∈ [
              {EXPORT_ELASTICITY_RANGE.join(', ')}].
            </p>
            <table className="data-table">
              <caption>Assumed elasticity pairs</caption>
              <thead>
                <tr>
                  <th scope="col">#</th>
                  <th scope="col">Import elasticity εM</th>
                  <th scope="col">Export elasticity εX</th>
                  <th scope="col">
                    <span className="sr-only">Actions</span>
                  </th>
                </tr>
              </thead>
              <tbody>
                {form.grid.map((g, i) => (
                  <tr key={i}>
                    <th scope="row">{i + 1}</th>
                    <td>
                      <input
                        type="text"
                        inputMode="decimal"
                        aria-label={`Import elasticity, row ${i + 1}`}
                        value={g.importElasticity}
                        onChange={(e) => setGrid(i, 'importElasticity', e.target.value)}
                      />
                      {showErr(`grid.${i}.import`)}
                    </td>
                    <td>
                      <input
                        type="text"
                        inputMode="decimal"
                        aria-label={`Export elasticity, row ${i + 1}`}
                        value={g.exportElasticity}
                        onChange={(e) => setGrid(i, 'exportElasticity', e.target.value)}
                      />
                      {showErr(`grid.${i}.export`)}
                    </td>
                    <td>
                      <button type="button" onClick={() => setForm({ ...form, grid: form.grid.filter((_, j) => j !== i) })} aria-label={`Remove row ${i + 1}`}>
                        Remove
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
            <button
              type="button"
              disabled={form.grid.length >= 100}
              onClick={() => setForm({ ...form, grid: [...form.grid, { importElasticity: '0', exportElasticity: '0' }] })}
            >
              Add pair
            </button>
            {showErr('grid')}
          </fieldset>
        )}

        <p>
          <button type="submit" className="primary" disabled={!canSubmit} aria-describedby="submit-hint">
            {busy === 'creating' ? 'Creating scenario…' : busy === 'starting' ? 'Submitting run…' : 'Create scenario and run'}
          </button>{' '}
          <span id="submit-hint" className="field-hint">
            {releaseError ? 'Blocked: no valid baseline release.' : !formValid ? 'Fix the highlighted assumptions.' : 'Runs as a queued job.'}
          </span>
        </p>
        {!!submitError && <MissingInputs error={submitError} />}
        {!!submitError && <ProblemView error={submitError} />}
      </form>

      <aside className="panel limits-panel" aria-labelledby="limits-h">
        <h2 id="limits-h">Limits of this model</h2>
        <ul>
          {LIMITS_TEXT.map((t) => (
            <li key={t}>{t}</li>
          ))}
        </ul>
      </aside>

      {simId && (
        <section className="panel" aria-labelledby="job-h">
          <h2 id="job-h">Run status</h2>
          <p role="status" aria-live="polite">
            Job <span className="mono">{shortId(simId)}</span>: {progress.label}
            {job && job.attempts > 0 && (
              <span className="muted">
                {' '}
                · attempt {job.attempts}/{job.max_attempts}
              </span>
            )}
          </p>
          {progress.fraction !== null && <progress value={progress.fraction} max={1} aria-label="Job progress" />}
          {running && !job?.cancellable && <Loading what="job" />}
          {job?.cancellable && (
            <button type="button" onClick={cancel}>
              Cancel run
            </button>
          )}
          {cancelState && <p role="status">{cancelState}</p>}
          {job?.last_error && (
            <div className="problem" role="alert">
              <p>
                <strong>Last error</strong>
              </p>
              <JsonView value={job.last_error} />
            </div>
          )}
          {!!jobError && <ErrorState error={jobError} what="Job status" />}
        </section>
      )}

      {simId && simEnv && <ResultsView simId={simId} env={simEnv} />}
    </div>
  );
}
