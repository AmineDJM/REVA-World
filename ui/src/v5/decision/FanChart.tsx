import type { PeriodValue } from './api';
import { bands, fmt, missingFr } from './model';

const W = 520;
const H = 180;
const PAD = { l: 52, r: 12, t: 12, b: 28 };

/**
 * Fan chart of one variable: 5–95 % and 25–75 % bands of the Monte-Carlo draws and the median, against zero (the
 * baseline path: the chart shows *changes*). Missing periods are marked and never drawn as zero; segments are not
 * joined across a missing period. A table gives the same values for screen readers.
 */
export function FanChart({ periods, unit, label }: { periods: PeriodValue[]; unit: string; label: string }) {
  const { drawn, missing } = bands(periods);
  const levels = periods.some((p) => p.scenario_level);
  const xs = periods.map((p) => p.period);
  const x = (i: number) => PAD.l + (xs.length <= 1 ? (W - PAD.l - PAD.r) / 2 : (i * (W - PAD.l - PAD.r)) / (xs.length - 1));
  const lo = Math.min(0, ...drawn.map((d) => d.p05));
  const hi = Math.max(0, ...drawn.map((d) => d.p95));
  const span = hi - lo || 1;
  const y = (v: number) => PAD.t + ((hi - v) * (H - PAD.t - PAD.b)) / span;
  const idx = new Map(xs.map((p, i) => [p, i] as const));
  // contiguous runs of drawn periods
  const runs: (typeof drawn)[] = [];
  let cur: typeof drawn = [];
  let prev = -2;
  for (const d of drawn) {
    const i = idx.get(d.period)!;
    if (i !== prev + 1 && cur.length) {
      runs.push(cur);
      cur = [];
    }
    cur.push(d);
    prev = i;
  }
  if (cur.length) runs.push(cur);
  const area = (run: typeof drawn, a: 'p05' | 'p25', b: 'p95' | 'p75') => {
    const top = run.map((d) => `${x(idx.get(d.period)!)},${y(d[b])}`);
    const bot = [...run].reverse().map((d) => `${x(idx.get(d.period)!)},${y(d[a])}`);
    return [...top, ...bot].join(' ');
  };
  return (
    <figure className="v5-fan">
      <svg viewBox={`0 0 ${W} ${H}`} role="img" aria-label={`${label} — éventail des tirages (5–95 %, 25–75 %, médiane), en ${unit}`} data-testid="fan-chart">
        <line x1={PAD.l} x2={W - PAD.r} y1={y(0)} y2={y(0)} className="v5-zero" />
        <text x={PAD.l - 6} y={y(0) + 4} textAnchor="end" className="v5-axis">
          0
        </text>
        <text x={PAD.l - 6} y={y(hi) + 10} textAnchor="end" className="v5-axis">
          {fmt(hi)}
        </text>
        {lo < 0 && (
          <text x={PAD.l - 6} y={y(lo)} textAnchor="end" className="v5-axis">
            {fmt(lo)}
          </text>
        )}
        {runs.map((run, k) =>
          run.length === 1 ? (
            <g key={k}>
              <line x1={x(idx.get(run[0]!.period)!)} x2={x(idx.get(run[0]!.period)!)} y1={y(run[0]!.p05)} y2={y(run[0]!.p95)} className="v5-band-outer-line" />
              <circle cx={x(idx.get(run[0]!.period)!)} cy={y(run[0]!.p50)} r={3} className="v5-median-dot" />
            </g>
          ) : (
            <g key={k}>
              <polygon points={area(run, 'p05', 'p95')} className="v5-band-outer" />
              <polygon points={area(run, 'p25', 'p75')} className="v5-band-inner" />
              <polyline points={run.map((d) => `${x(idx.get(d.period)!)},${y(d.p50)}`).join(' ')} className="v5-median" />
            </g>
          ),
        )}
        {missing.map((m) => (
          <g key={m.period} className="v5-missing-mark">
            <line x1={x(idx.get(m.period)!)} x2={x(idx.get(m.period)!)} y1={PAD.t} y2={H - PAD.b} />
          </g>
        ))}
        {xs.map((p, i) => (
          <text key={p} x={x(i)} y={H - 8} textAnchor="middle" className="v5-axis">
            {p}
          </text>
        ))}
      </svg>
      <details>
        <summary>Valeurs ({unit})</summary>
        <table className="v5-values">
          <thead>
            <tr>
              <th scope="col">Période</th>
              <th scope="col">5 %</th>
              <th scope="col">médiane</th>
              <th scope="col">95 %</th>
              {levels && <th scope="col">niveau scénario (médiane)</th>}
            </tr>
          </thead>
          <tbody>
            {periods.map((p) => (
              <tr key={p.period}>
                <th scope="row">{p.period}</th>
                {p.missing_reason ? (
                  <td colSpan={levels ? 4 : 3} className="v5-missing">
                    non calculé : {missingFr(p.missing_reason)}
                  </td>
                ) : (
                  <>
                    <td>{fmt(p.p05)}</td>
                    <td>{fmt(p.p50)}</td>
                    <td>{fmt(p.p95)}</td>
                    {levels && <td title={p.scenario_level?.note ?? undefined}>{p.scenario_level ? fmt(p.scenario_level.p50) : '—'}</td>}
                  </>
                )}
              </tr>
            ))}
          </tbody>
        </table>
      </details>
    </figure>
  );
}

/** Undated result (cumulative, long-run, first-round...): one interval, no time axis. */
export function IntervalBar({ q, unit, label, kind }: { q: { p05: number; p25: number; p50: number; p75: number; p95: number }; unit: string; label: string; kind: string }) {
  const lo = Math.min(0, q.p05);
  const hi = Math.max(0, q.p95);
  const span = hi - lo || 1;
  const sx = (v: number) => 12 + ((v - lo) * (W - 24)) / span;
  return (
    <figure className="v5-fan">
      <svg viewBox={`0 0 ${W} 56`} role="img" aria-label={`${label} — ${kind} : médiane ${fmt(q.p50)} ${unit}, 5–95 % de ${fmt(q.p05)} à ${fmt(q.p95)}`} data-testid="interval-bar">
        <line x1={sx(0)} x2={sx(0)} y1={6} y2={42} className="v5-zero" />
        <rect x={sx(q.p05)} width={Math.max(1, sx(q.p95) - sx(q.p05))} y={14} height={16} className="v5-band-outer" />
        <rect x={sx(q.p25)} width={Math.max(1, sx(q.p75) - sx(q.p25))} y={14} height={16} className="v5-band-inner" />
        <line x1={sx(q.p50)} x2={sx(q.p50)} y1={10} y2={34} className="v5-median" />
        <text x={sx(q.p05)} y={52} textAnchor="middle" className="v5-axis">
          {fmt(q.p05)}
        </text>
        <text x={sx(q.p95)} y={52} textAnchor="middle" className="v5-axis">
          {fmt(q.p95)}
        </text>
      </svg>
    </figure>
  );
}
