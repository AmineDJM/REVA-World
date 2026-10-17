import { useEffect, useMemo, useRef, useState } from 'react';
import type uPlot from 'uplot';
import 'uplot/dist/uPlot.min.css';
import { installLocaleGuard } from '../../../lib/locale';
import { frCompact, frDecimal } from '../../shell/ui';

export interface TimePoint {
  t: string; // ISO date of the period start
  t_end?: string | null;
  v: string | null; // exact decimal string
  projection?: boolean;
  cited?: boolean;
  source?: string | null;
}

function cssVar(name: string, fallback: string): string {
  try {
    return getComputedStyle(document.documentElement).getPropertyValue(name).trim() || fallback;
  } catch {
    return fallback;
  }
}

/** Seconds since epoch of an ISO date (UTC), or null. */
export function epoch(t: string): number | null {
  const m = /^(-?\d{4})(?:-(\d{2}))?(?:-(\d{2}))?/.exec(t);
  if (!m) return null;
  const ms = Date.UTC(Number(m[1]), Number(m[2] ?? '1') - 1, Number(m[3] ?? '1'));
  return Number.isFinite(ms) ? ms / 1000 : null;
}

/** Period label of a point: a year, a month or a day (as published). */
export function periodLabel(p: TimePoint): string {
  const s = p.t ?? '';
  const e = p.t_end ?? '';
  if (/^\d{4}-01-01$/.test(s) && /^\d{4}-01-01$/.test(e) && Number(e.slice(0, 4)) - Number(s.slice(0, 4)) === 1) return s.slice(0, 4);
  if (/-01$/.test(s) && /-01$/.test(e) && e > s && e.slice(0, 7) !== s.slice(0, 7) && Number(e.slice(8)) === 1 && Date.parse(e) - Date.parse(s) < 32 * 864e5) return s.slice(0, 7);
  return s.slice(0, 10);
}

/**
 * One series over time (uPlot): observations solid, projections dashed and never joined into the observed line,
 * the points the answer cited marked; a hover readout gives value, period and source. Gaps are gaps, never 0.
 */
export function TimeChart({ points, unit, label, height = 220 }: { points: TimePoint[]; unit?: string | null; label: string; height?: number }) {
  const el = useRef<HTMLDivElement>(null);
  const [fallback, setFallback] = useState(false);
  const [hover, setHover] = useState<number | null>(null);
  const rows = useMemo(
    () =>
      points
        .map((p) => ({ p, x: epoch(p.t), y: p.v === null ? null : Number(p.v) }))
        .filter((r): r is { p: TimePoint; x: number; y: number | null } => r.x !== null)
        .sort((a, b) => a.x - b.x),
    [points],
  );

  useEffect(() => {
    const node = el.current;
    if (!node) return;
    let ctx: CanvasRenderingContext2D | null = null;
    try {
      ctx = typeof ResizeObserver === 'undefined' ? null : document.createElement('canvas').getContext('2d');
    } catch {
      ctx = null;
    }
    if (!ctx || rows.length < 2) {
      setFallback(true);
      return;
    }
    const xs = rows.map((r) => r.x);
    const obs = rows.map((r) => (r.p.projection ? null : r.y));
    const firstProj = rows.findIndex((r) => r.p.projection);
    const proj = rows.map((r, i) => (r.p.projection || (firstProj > 0 && i === firstProj - 1) ? r.y : null));
    const cited = rows.map((r) => (r.p.cited ? r.y : null));
    const line = cssVar('--v8-observed', '#1f5fbf');
    const projC = cssVar('--v8-projection', '#7c3fc4');
    const accent = cssVar('--v8-accent', '#3653d3');
    const grid = cssVar('--v8-border', '#e2e4e8');
    const ink = cssVar('--v8-fg-muted', '#565d6a');
    const dense = rows.length > 60;
    let plot: uPlot | null = null;
    let ro: ResizeObserver | null = null;
    let cancelled = false;
    installLocaleGuard();
    import('uplot')
      .then(({ default: UPlot }) => {
        if (cancelled) return;
        plot = new UPlot(
          {
            width: Math.max(260, node.clientWidth || 600),
            height,
            series: [
              {},
              { label, stroke: line, width: 2, spanGaps: false, points: { show: !dense, size: 4, fill: line } },
              { label: `${label} (projection)`, stroke: projC, width: 1.5, dash: [5, 4], spanGaps: false, points: { show: false } },
              { label: 'cité', stroke: 'transparent', width: 0, points: { show: true, size: 9, stroke: accent, fill: cssVar('--v8-surface', '#fff'), width: 2 } },
            ],
            scales: { x: { time: true } },
            axes: [
              { stroke: ink, grid: { stroke: grid, width: 1 }, ticks: { stroke: grid } },
              { stroke: ink, grid: { stroke: grid, width: 1 }, ticks: { stroke: grid }, size: 52, values: (_u, vals) => vals.map((v) => frCompact(v)) },
            ],
            legend: { show: false },
            cursor: { drag: { x: false, y: false }, points: { size: 7 } },
            hooks: {
              setCursor: [
                (u) => {
                  const i = u.cursor.idx;
                  setHover(i === null || i === undefined ? null : i);
                },
              ],
            },
          },
          [xs, obs, proj, cited] as uPlot.AlignedData,
          node,
        );
        ro = new ResizeObserver(() => plot?.setSize({ width: Math.max(260, node.clientWidth), height }));
        ro.observe(node);
      })
      .catch(() => !cancelled && setFallback(true));
    return () => {
      cancelled = true;
      ro?.disconnect();
      plot?.destroy();
    };
  }, [rows, height, label]);

  const shown = hover !== null ? rows[hover] : ([...rows].reverse().find((r) => r.p.cited) ?? rows[rows.length - 1]);
  return (
    <div className="v8d-chart">
      <div className="v8d-readout" aria-live="polite">
        {shown && (
          <>
            <span className="v8-num v8d-readout-v">{shown.p.v === null ? 'manquant' : frDecimal(shown.p.v, 3)}</span>
            {unit && <span className="v8-subtle"> {unit}</span>}
            <span className="v8-subtle"> · {periodLabel(shown.p)}</span>
            {shown.p.projection && <span className="v8d-proj-tag"> projection</span>}
            {shown.p.source && <span className="v8-subtle"> · {shown.p.source}</span>}
          </>
        )}
      </div>
      {fallback ? (
        <table className="v8d-table">
          <caption className="v8-sr-only">{label}</caption>
          <thead>
            <tr>
              <th scope="col">Période</th>
              <th scope="col">Valeur</th>
            </tr>
          </thead>
          <tbody>
            {rows.slice(-24).map((r) => (
              <tr key={r.p.t} className={r.p.cited ? 'is-cited' : undefined}>
                <td>{periodLabel(r.p)}</td>
                <td className="v8-num">
                  {r.p.v === null ? 'manquant' : frDecimal(r.p.v, 3)}
                  {r.p.projection ? ' (projection)' : ''}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      ) : (
        <div ref={el} className="v8d-plot" role="img" aria-label={`${label}${unit ? ` (${unit})` : ''} : ${rows.length} points ; la vue tableau du détail des preuves donne les valeurs citées.`} />
      )}
    </div>
  );
}
