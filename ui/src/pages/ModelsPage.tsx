import { useMemo, useState, type FormEvent } from 'react';
import { getModelEvaluations, getModels, newIdempotencyKey, startReplay } from '../api/client';
import type { Envelope, ModelEvaluation, ModelVersionOut, ReplayMode } from '../api/types';
import { ContextBanner } from '../components/ContextBanner';
import { EnvelopeDetails } from '../components/EnvelopeDetails';
import { JobTracker } from '../components/JobTracker';
import { JsonView } from '../components/JsonView';
import { Empty, ErrorState, Loading, ProblemView } from '../components/StateViews';
import { formatDateTime, humanize, localUtcToIso, shortId } from '../lib/format';
import { useApi } from '../lib/useApi';

export const REPLAY_MODE_LABEL: Record<ReplayMode, string> = {
  operational_as_known: 'operational as-known — what REVA actually had published at the cutoff',
  available_then_reconstruction: 'available-then reconstruction — archived vintages; not what REVA believed then',
  revised_data_retrospective: 'revised-data retrospective — not a real-time backtest',
};

export function replayModeLabel(mode: string): string {
  return (REPLAY_MODE_LABEL as Record<string, string>)[mode] ?? humanize(mode);
}

function metric(m: Record<string, unknown>, k: string): string {
  const v = m[k];
  if (v === null || v === undefined) return 'not defined';
  if (typeof v === 'number') return Number.isFinite(v) ? v.toLocaleString('en-US', { maximumSignificantDigits: 4 }) : 'not defined';
  return String(v);
}

interface Tournament {
  manifest: string;
  replayMode: string;
  label: string | null;
  period: string;
  recordedAt: string;
  rows: { evaluation: ModelEvaluation; model: ModelVersionOut | undefined }[];
}

function groupTournaments(evals: ModelEvaluation[], models: Map<string, ModelVersionOut>): Tournament[] {
  const by = new Map<string, Tournament>();
  for (const e of evals) {
    const period = String(e.metrics.period ?? e.task_scope.period ?? 'unspecified');
    const key = `${e.evaluation_manifest_sha256}|${period}`;
    const t =
      by.get(key) ??
      ({
        manifest: e.evaluation_manifest_sha256,
        replayMode: e.replay_mode,
        label: typeof e.metrics.label === 'string' ? e.metrics.label : null,
        period,
        recordedAt: e.recorded_at,
        rows: [],
      } as Tournament);
    if (!t.rows.some((r) => r.evaluation.id === e.id)) t.rows.push({ evaluation: e, model: models.get(e.model_version_id) });
    by.set(key, t);
  }
  return [...by.values()].sort((a, b) => b.recordedAt.localeCompare(a.recordedAt));
}

function TournamentTable({ t }: { t: Tournament }) {
  const hasNaive = t.rows.some((r) => r.evaluation.metrics.is_naive_baseline === true);
  const rows = [...t.rows].sort(
    (a, b) => Number(b.evaluation.metrics.is_naive_baseline === true) - Number(a.evaluation.metrics.is_naive_baseline === true),
  );
  return (
    <article className="panel" aria-label={`Tournament ${shortId(t.manifest)}`}>
      <h3>
        Tournament <span className="mono">{shortId(t.manifest)}</span> · {humanize(t.period)}
      </h3>
      <p>
        <strong>Replay mode:</strong> {replayModeLabel(t.replayMode)}
      </p>
      {t.label && <p className="muted">{t.label}</p>}
      {!hasNaive && (
        <p className="bound-flag" role="alert">
          Naive baseline missing from this evaluation set — scores cannot be compared to the naive forecast.
        </p>
      )}
      <div className="table-wrap">
        <table className="data-table">
          <caption>Scores (recorded {formatDateTime(t.recordedAt)}); naive baseline listed first</caption>
          <thead>
            <tr>
              <th scope="col">Model</th>
              <th scope="col">Role</th>
              <th scope="col">MASE</th>
              <th scope="col">MAE</th>
              <th scope="col">Predictions</th>
              <th scope="col">Interval coverage</th>
              <th scope="col">Decision</th>
            </tr>
          </thead>
          <tbody>
            {rows.map(({ evaluation: e, model }) => {
              const m = e.metrics;
              const naive = m.is_naive_baseline === true;
              return (
                <tr key={e.id}>
                  <th scope="row">
                    {model ? `${model.model_key}@${model.version}` : shortId(e.model_version_id)}
                    {naive && <span className="tag"> naive baseline</span>}
                  </th>
                  <td>{humanize(String(m.role ?? '—'))}</td>
                  <td className="numeric">{metric(m, 'mase')}</td>
                  <td className="numeric">{metric(m, 'mae')}</td>
                  <td className="numeric">{metric(m, 'n_predictions')}</td>
                  <td className="numeric">
                    {typeof m.coverage === 'number' ? `${metric(m, 'coverage')} (${metric(m, 'n_intervals')} intervals)` : 'no intervals'}
                  </td>
                  <td>{m.decision ? humanize(String(m.decision)) : '—'}</td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
      <details>
        <summary>Subgroups, ineligible cases and promotions</summary>
        {rows.map(({ evaluation: e, model }) => (
          <div key={e.id}>
            <h4>{model ? model.model_key : shortId(e.model_version_id)}</h4>
            <JsonView value={{ subgroups: e.metrics.subgroups, ineligible: e.metrics.ineligible, promotions: e.promotions, task_scope: e.task_scope }} />
          </div>
        ))}
      </details>
    </article>
  );
}

function ReplayForm() {
  const [mode, setMode] = useState<ReplayMode>('operational_as_known');
  const [cutoff, setCutoff] = useState('');
  const [concepts, setConcepts] = useState('');
  const [subjects, setSubjects] = useState('FRA');
  const [jobId, setJobId] = useState<string | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [busy, setBusy] = useState(false);

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    const iso = localUtcToIso(cutoff);
    if (!iso) {
      setError(new Error('Enter a knowledge cutoff (UTC).'));
      return;
    }
    setBusy(true);
    setError(null);
    try {
      const split = (s: string) => s.split(',').map((x) => x.trim()).filter(Boolean);
      const r = await startReplay({ mode, cutoff: iso, concepts: split(concepts), subjects: split(subjects) }, newIdempotencyKey());
      setJobId(r.job_id);
    } catch (err) {
      setError(err);
    } finally {
      setBusy(false);
    }
  };

  return (
    <form onSubmit={submit} aria-label="Start a replay" className="form-grid">
      <label>
        Replay mode
        <select value={mode} onChange={(e) => setMode(e.target.value as ReplayMode)}>
          {(Object.keys(REPLAY_MODE_LABEL) as ReplayMode[]).map((m) => (
            <option key={m} value={m}>
              {humanize(m)}
            </option>
          ))}
        </select>
        <span className="field-hint">{REPLAY_MODE_LABEL[mode]}</span>
      </label>
      <label>
        Knowledge cutoff (UTC)
        <input type="datetime-local" value={cutoff} onChange={(e) => setCutoff(e.target.value)} required />
      </label>
      <label>
        Concepts (comma-separated, optional)
        <input type="text" value={concepts} onChange={(e) => setConcepts(e.target.value)} />
      </label>
      <label>
        Subjects (ISO3 or UUID, comma-separated)
        <input type="text" value={subjects} onChange={(e) => setSubjects(e.target.value)} />
      </label>
      <div>
        <button type="submit" disabled={busy}>
          {busy ? 'Submitting…' : 'Start replay'}
        </button>
      </div>
      {!!error && <ProblemView error={error} />}
      {jobId && <JobTracker jobId={jobId} label="Replay" />}
    </form>
  );
}

export default function ModelsPage() {
  const models = useApi((s) => getModels(undefined, s), []);
  const [selected, setSelected] = useState<string | null>(null);

  const list = models.data?.data ?? [];
  const byId = useMemo(() => new Map(list.map((m) => [m.id, m])), [list]);
  const evaluated = useMemo(() => list.filter((m) => m.evaluation_count > 0).slice(0, 20), [list]);

  const evals = useApi(
    async (s) => {
      const envs: Envelope<ModelEvaluation[]>[] = await Promise.all(evaluated.map((m) => getModelEvaluations(m.id, { limit: 200 }, s)));
      return { envs, all: envs.flatMap((e) => e.data) };
    },
    [evaluated.map((m) => m.id).join(',')],
    evaluated.length > 0,
  );
  const tournaments = useMemo(() => groupTournaments(evals.data?.all ?? [], byId), [evals.data, byId]);
  const chosen = list.find((m) => m.id === selected);

  return (
    <>
      <h1>Models</h1>
      <ContextBanner meta={evals.data?.envs[0] ?? models.data} scope="Model registry and evaluations" />

      <section className="panel" aria-labelledby="registry-h">
        <h2 id="registry-h">Registered model versions</h2>
        {models.status === 'loading' && <Loading what="models" />}
        {models.status === 'error' && <ErrorState error={models.error} what="Models" onRetry={models.reload} />}
        {models.data && list.length === 0 && <Empty what="model versions" />}
        {list.length > 0 && (
          <div className="table-wrap">
            <table className="data-table">
              <caption>Model versions (select for scorecard)</caption>
              <thead>
                <tr>
                  <th scope="col">Model</th>
                  <th scope="col">Kind</th>
                  <th scope="col">Approval</th>
                  <th scope="col">Training cutoff</th>
                  <th scope="col">Evaluations</th>
                  <th scope="col">
                    <span className="sr-only">Actions</span>
                  </th>
                </tr>
              </thead>
              <tbody>
                {list.map((m) => (
                  <tr key={m.id} className={selected === m.id ? 'selected' : undefined}>
                    <th scope="row">
                      {m.model_key}@{m.version}
                    </th>
                    <td>{humanize(m.kind)}</td>
                    <td>{humanize(m.approval_status)}</td>
                    <td>{m.training_cutoff ? formatDateTime(m.training_cutoff) : 'not applicable'}</td>
                    <td className="numeric">{m.evaluation_count}</td>
                    <td>
                      <button type="button" className="link-button" onClick={() => setSelected(m.id)} aria-pressed={selected === m.id}>
                        Scorecard
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
        {models.data && <EnvelopeDetails meta={models.data} />}
      </section>

      {chosen && (
        <section className="panel" aria-labelledby="scorecard-h">
          <h2 id="scorecard-h">
            Scorecard: {chosen.model_key}@{chosen.version}
          </h2>
          <dl className="kv">
            <div className="kv-row">
              <dt>Applicability</dt>
              <dd>
                <JsonView value={chosen.applicability} />
              </dd>
            </div>
            <div className="kv-row">
              <dt>Assumptions</dt>
              <dd>
                <JsonView value={chosen.assumptions} />
              </dd>
            </div>
            <div className="kv-row">
              <dt>Inputs</dt>
              <dd>
                <JsonView value={chosen.input_ports} />
              </dd>
            </div>
            <div className="kv-row">
              <dt>Outputs</dt>
              <dd>
                <JsonView value={chosen.output_ports} />
              </dd>
            </div>
            <div className="kv-row">
              <dt>Spec digest</dt>
              <dd className="mono break">{chosen.spec_sha256}</dd>
            </div>
          </dl>
          {chosen.evaluation_count === 0 && <p className="muted">No evaluation recorded: historical validation is not assessed.</p>}
          {tournaments
            .filter((t) => t.rows.some((r) => r.evaluation.model_version_id === chosen.id))
            .map((t) => (
              <p key={`${t.manifest}-${t.period}`}>
                {humanize(t.period)} ({replayModeLabel(t.replayMode)}):{' '}
                {(() => {
                  const r = t.rows.find((x) => x.evaluation.model_version_id === chosen.id)!;
                  return `MASE ${metric(r.evaluation.metrics, 'mase')}, decision ${humanize(String(r.evaluation.metrics.decision ?? '—'))}`;
                })()}
              </p>
            ))}
        </section>
      )}

      <section className="panel" aria-labelledby="tournaments-h">
        <h2 id="tournaments-h">Tournament results</h2>
        {evals.status === 'loading' && <Loading what="evaluations" />}
        {evals.status === 'error' && <ErrorState error={evals.error} what="Evaluations" onRetry={evals.reload} />}
        {models.data && evaluated.length === 0 && (
          <Empty what="evaluations">
            <p>No model has been evaluated yet. Historical validation is not assessed for any model.</p>
          </Empty>
        )}
        {tournaments.map((t) => (
          <TournamentTable key={`${t.manifest}-${t.period}`} t={t} />
        ))}
      </section>

      <section className="panel" aria-labelledby="replay-h">
        <h2 id="replay-h">Replay</h2>
        <ReplayForm />
      </section>
    </>
  );
}
