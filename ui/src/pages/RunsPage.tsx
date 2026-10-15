import { useMemo, useState } from 'react';
import { Link } from 'react-router';
import { getJobs, getScenarios } from '../api/client';
import { MODEL_KEYS } from '../api/v2types';
import { ContextBanner } from '../components/ContextBanner';
import { Empty, ErrorState, Loading } from '../components/StateViews';
import { formatDateTime, humanize, shortId } from '../lib/format';
import { useApi } from '../lib/useApi';

const MODEL_LABEL: Record<string, string> = {
  [MODEL_KEYS.crude]: 'Crude border-price exposure',
  [MODEL_KEYS.cohort]: 'Cohort-component population projection',
  [MODEL_KEYS.io]: 'Input-output price propagation',
  [MODEL_KEYS.retirement]: 'Retirement age',
};

/** Every simulation job with its scenario's model, newest first. */
export default function RunsPage() {
  const jobs = useApi((s) => getJobs({ job_type: 'simulation', limit: 200 }, s), []);
  const scenarios = useApi((s) => getScenarios({ limit: 1000 }, s), []);
  const [model, setModel] = useState('');
  const rows = useMemo(() => {
    const byId = new Map((scenarios.data?.data ?? []).map((s) => [s.id, s]));
    return (jobs.data?.data ?? []).map((j) => {
      const sc = byId.get(String(j.payload?.scenario_id ?? '')) ?? null;
      return { job: j, scenario: sc, model: sc ? String(sc.spec.model_key ?? '') : '' };
    });
  }, [jobs.data, scenarios.data]);
  const models = useMemo(() => [...new Set(rows.map((r) => r.model).filter(Boolean))].sort(), [rows]);
  const shown = rows.filter((r) => !model || r.model === model);

  return (
    <>
      <h1>Simulation runs</h1>
      <ContextBanner meta={scenarios.data ?? jobs.data} simulation scope="All simulation runs (jobs) and their scenarios" />
      <section className="panel" aria-labelledby="runs-h">
        <h2 id="runs-h">Runs</h2>
        {(jobs.status === 'loading' || scenarios.status === 'loading') && <Loading what="runs" />}
        {jobs.status === 'error' && <ErrorState error={jobs.error} what="Runs" onRetry={jobs.reload} />}
        {scenarios.status === 'error' && <ErrorState error={scenarios.error} what="Scenarios" onRetry={scenarios.reload} />}
        {models.length > 1 && (
          <label>
            Model
            <select value={model} onChange={(e) => setModel(e.target.value)}>
              <option value="">All models</option>
              {models.map((m) => (
                <option key={m} value={m}>
                  {MODEL_LABEL[m] ?? m}
                </option>
              ))}
            </select>
          </label>
        )}
        {jobs.data && shown.length === 0 && (
          <Empty what="simulation runs">
            <p>
              No run yet. Start one from the <Link to="/compose">composer</Link> or the <Link to="/simulation">Simulation</Link> form.
            </p>
          </Empty>
        )}
        {shown.length > 0 && (
          <div className="table-wrap">
            <table className="data-table" data-testid="runs-table">
              <caption>
                {shown.length} run{shown.length === 1 ? '' : 's'}
                {jobs.data?.next_cursor ? ' (more exist; newest 200 shown)' : ''}
              </caption>
              <thead>
                <tr>
                  <th scope="col">Run</th>
                  <th scope="col">Scenario</th>
                  <th scope="col">Model</th>
                  <th scope="col">Fidelity</th>
                  <th scope="col">Status</th>
                  <th scope="col">Created</th>
                </tr>
              </thead>
              <tbody>
                {shown.map((r) => (
                  <tr key={r.job.id}>
                    <th scope="row">
                      <Link to={`/runs/${r.job.id}`} className="mono">
                        {shortId(r.job.id)}
                      </Link>
                    </th>
                    <td>{r.scenario?.title ?? <span className="muted">not listed</span>}</td>
                    <td>
                      {r.model ? MODEL_LABEL[r.model] ?? r.model : '—'}
                      {r.model && r.model !== MODEL_KEYS.crude && <span className="badge badge-experimental">experimental</span>}
                    </td>
                    <td>{String(r.job.payload?.fidelity ?? '—')}</td>
                    <td>{humanize(r.job.status)}</td>
                    <td>{formatDateTime(r.job.created_at)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>
    </>
  );
}
