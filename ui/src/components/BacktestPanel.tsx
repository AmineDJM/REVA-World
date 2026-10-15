import { displayDecimal } from '../lib/format';
import type { Backtest } from '../lib/runviews';

/**
 * Back-test results next to the model: predicted vs observed and the served error, including
 * when the error is large. The UI never recomputes, rounds away or hides an error.
 */
export function BacktestPanel({ backtests, source, unitRef }: { backtests: Backtest[]; source?: string; unitRef?: string }) {
  return (
    <section className="panel backtest-panel" aria-labelledby="backtest-h" data-testid="backtest-panel">
      <h2 id="backtest-h">Back-test (out of sample)</h2>
      <p className="field-hint">
        Predicted versus observed for a past period the model was not fitted on. Errors are shown as served, good or bad; they describe past
        performance of the method, not the uncertainty of this projection.
      </p>
      {backtests.length === 0 && (
        <p className="muted" role="status" data-testid="backtest-absent">
          No back-test is attached to this run{source ? `; see ${source}` : ''}. Its absence is not evidence of accuracy.
        </p>
      )}
      {backtests.map((b, i) => (
        <div key={i} className="table-wrap">
          <table className="data-table">
            <caption>{b.title || 'Back-test'}</caption>
            <thead>
              <tr>
                <th scope="col">Group</th>
                <th scope="col">Predicted</th>
                <th scope="col">Observed</th>
                <th scope="col">{b.rows[0]?.errorLabel ?? 'Error'}</th>
              </tr>
            </thead>
            <tbody>
              {b.rows.map((r) => (
                <tr key={r.label}>
                  <th scope="row">{r.label}</th>
                  <td className="num">{r.predicted === null ? <em>not reported</em> : displayDecimal(r.predicted, unitRef).text}</td>
                  <td className="num">{r.observed === null ? <em>not reported</em> : displayDecimal(r.observed, unitRef).text}</td>
                  <td className="num backtest-error" data-error={r.error ?? ''}>
                    {r.error === null ? <em>not reported</em> : `${r.error}${/%/.test(r.errorLabel) ? ' %' : ''}`}
                    {r.extra && <span className="field-hint"> · {r.extra}</span>}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
          {b.benchmark && <p className="field-hint">Benchmark: {b.benchmark}</p>}
          {b.notes.length > 0 && (
            <ul className="field-hint">
              {b.notes.map((n, j) => (
                <li key={j}>{n}</li>
              ))}
            </ul>
          )}
        </div>
      ))}
    </section>
  );
}
