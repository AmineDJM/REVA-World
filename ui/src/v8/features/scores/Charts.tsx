/**
 * Small inline-SVG charts of the scoreboard, drawn only from the points the API copied from result files:
 * ratio by origin month / delay / horizon (reference line at 1 = same error as the official forecaster),
 * PIT histogram (flat line = calibrated), interval coverage (observed vs nominal).
 */
import type { Chart } from './api';
import { fmtPct, fmtRatio } from './model';

const W = 320;
const H = 150;
const PAD = { l: 38, r: 10, t: 10, b: 28 };

type Pt = { x: number; y: number; n?: number | null };

function asPoints(c: Chart): Pt[] {
  return (c.points ?? [])
    .map((p) => p as { x?: unknown; y?: unknown; n?: unknown })
    .filter((p) => typeof p.x === 'number' && typeof p.y === 'number' && Number.isFinite(p.y))
    .map((p) => ({ x: p.x as number, y: p.y as number, n: typeof p.n === 'number' ? p.n : null }));
}

export function RatioChart({ chart }: { chart: Chart }) {
  const pts = asPoints(chart);
  if (pts.length === 0) return null;
  const xs = pts.map((p) => p.x);
  const ys = pts.map((p) => p.y);
  const x0 = Math.min(...xs);
  const x1 = Math.max(...xs);
  const y0 = Math.min(0.9, ...ys) * 0.95;
  const y1 = Math.max(1.1, ...ys) * 1.05;
  const sx = (x: number) => PAD.l + (x1 === x0 ? 0.5 : (x - x0) / (x1 - x0)) * (W - PAD.l - PAD.r);
  const sy = (y: number) => PAD.t + (1 - (y - y0) / (y1 - y0)) * (H - PAD.t - PAD.b);
  const ticks = [y0, 1, y1].map((v) => Number(v.toFixed(2)));
  const d = pts.map((p, i) => `${i ? 'L' : 'M'}${sx(p.x).toFixed(1)},${sy(p.y).toFixed(1)}`).join(' ');
  return (
    <figure className="sc-chart" data-testid="ratio-chart">
      <figcaption className="sc-chart-title">{chart.title_fr}</figcaption>
      <svg viewBox={`0 0 ${W} ${H}`} role="img" aria-label={`${chart.title_fr} : ${pts.map((p) => `${p.x} ${fmtRatio(p.y)}`).join(', ')}`}>
        <rect x={PAD.l} y={sy(1)} width={W - PAD.l - PAD.r} height={Math.max(0, sy(y0) - sy(1))} className="sc-chart-better" />
        <line x1={PAD.l} x2={W - PAD.r} y1={sy(1)} y2={sy(1)} className="sc-chart-ref" />
        {ticks.map((t) => (
          <text key={t} x={PAD.l - 4} y={sy(t) + 3} textAnchor="end" className="sc-chart-tick">
            {t.toLocaleString('fr-FR')}
          </text>
        ))}
        <path d={d} className="sc-chart-line" />
        {pts.map((p) => (
          <circle key={p.x} cx={sx(p.x)} cy={sy(p.y)} r={2.6} className="sc-chart-dot">
            <title>{`${p.x} : ${fmtRatio(p.y)}${p.n ? ` (n ${p.n})` : ''}`}</title>
          </circle>
        ))}
        {[...new Set(xs)].map((x) => (
          <text key={x} x={sx(x)} y={H - PAD.b + 13} textAnchor="middle" className="sc-chart-tick">
            {x}
          </text>
        ))}
        <text x={W - PAD.r} y={H - 4} textAnchor="end" className="sc-chart-axis">
          {chart.x_label_fr}
        </text>
      </svg>
      <p className="sc-chart-note">Ratio = erreur REVA / erreur officielle ; zone teintée = REVA meilleure. Descriptif, pas un test.</p>
    </figure>
  );
}

export function PitChart({ chart }: { chart: Chart }) {
  const counts = chart.counts ?? [];
  if (counts.length === 0) return null;
  const n = counts.reduce((a, b) => a + b, 0);
  const max = Math.max(...counts, n / counts.length) * 1.1;
  const bw = (W - PAD.l - PAD.r) / counts.length;
  const sy = (v: number) => PAD.t + (1 - v / max) * (H - PAD.t - PAD.b);
  const flat = sy(n / counts.length);
  return (
    <figure className="sc-chart" data-testid="pit-chart">
      <figcaption className="sc-chart-title">{chart.title_fr}</figcaption>
      <svg viewBox={`0 0 ${W} ${H}`} role="img" aria-label={`Histogramme PIT, ${counts.length} classes, n ${n}`}>
        {counts.map((c, i) => (
          <rect key={i} x={PAD.l + i * bw + 1} y={sy(c)} width={bw - 2} height={Math.max(0, H - PAD.b - sy(c))} className="sc-chart-bar">
            <title>{`PIT ${(i / counts.length).toFixed(1)}–${((i + 1) / counts.length).toFixed(1)} : ${c}`}</title>
          </rect>
        ))}
        <line x1={PAD.l} x2={W - PAD.r} y1={flat} y2={flat} className="sc-chart-ref" />
        <text x={PAD.l} y={H - PAD.b + 13} className="sc-chart-tick">0</text>
        <text x={W - PAD.r} y={H - PAD.b + 13} textAnchor="end" className="sc-chart-tick">1</text>
        <text x={W - PAD.r} y={H - 4} textAnchor="end" className="sc-chart-axis">
          PIT (plat = calibré)
        </text>
      </svg>
      <p className="sc-chart-note">
        n {n.toLocaleString('fr-FR')}
        {chart.coverage80 != null ? ` · couverture 80 % observée ${fmtPct(chart.coverage80)}` : ''}
      </p>
    </figure>
  );
}

export function CoverageChart({ chart }: { chart: Chart }) {
  const pts = (chart.points ?? []).map((p) => p as { row?: string; label?: string; nominal?: number; observed?: number | null; benchmark_observed?: number | null }).filter((p) => typeof p.nominal === 'number' && typeof p.observed === 'number');
  if (pts.length === 0) return null;
  const rowH = 18;
  const h = PAD.t + pts.length * rowH + 22;
  const w = 520;
  const l = 220;
  const sx = (v: number) => l + v * (w - l - 12);
  return (
    <figure className="sc-chart sc-chart-wide" data-testid="coverage-chart">
      <figcaption className="sc-chart-title">{chart.title_fr}</figcaption>
      <svg viewBox={`0 0 ${w} ${h}`} role="img" aria-label="Couverture observée contre nominale par ligne">
        {[0, 0.5, 0.8, 0.9, 1].map((t) => (
          <g key={t}>
            <line x1={sx(t)} x2={sx(t)} y1={PAD.t - 4} y2={h - 18} className="sc-chart-grid" />
            <text x={sx(t)} y={h - 6} textAnchor="middle" className="sc-chart-tick">{`${t * 100} %`}</text>
          </g>
        ))}
        {pts.map((p, i) => {
          const y = PAD.t + i * rowH + rowH / 2;
          return (
            <g key={`${p.row}-${p.nominal}`}>
              <text x={l - 6} y={y + 3} textAnchor="end" className="sc-chart-label">
                {(p.label ?? p.row ?? '').slice(0, 40)} · {Math.round((p.nominal as number) * 100)} %
              </text>
              <line x1={sx(p.nominal as number)} x2={sx(p.observed as number)} y1={y} y2={y} className="sc-cov-gap" />
              <line x1={sx(p.nominal as number)} x2={sx(p.nominal as number)} y1={y - 5} y2={y + 5} className="sc-cov-nominal" />
              {typeof p.benchmark_observed === 'number' && (
                <circle cx={sx(p.benchmark_observed)} cy={y} r={3.2} className="sc-cov-bench">
                  <title>{`officiel : ${fmtPct(p.benchmark_observed)}`}</title>
                </circle>
              )}
              <circle cx={sx(p.observed as number)} cy={y} r={3.6} className="sc-cov-obs">
                <title>{`observée ${fmtPct(p.observed)} pour ${fmtPct(p.nominal)} nominal`}</title>
              </circle>
            </g>
          );
        })}
      </svg>
      <p className="sc-chart-note">Trait = nominal ; point plein = REVA observé ; point creux = officiel observé (quand publié).</p>
    </figure>
  );
}

export function ChartView({ chart }: { chart: Chart }) {
  if (chart.kind === 'ratio_by') return <RatioChart chart={chart} />;
  if (chart.kind === 'pit') return <PitChart chart={chart} />;
  if (chart.kind === 'coverage') return <CoverageChart chart={chart} />;
  return null;
}
